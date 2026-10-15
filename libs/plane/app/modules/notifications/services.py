import datetime as dt

from urllib.parse import urlparse

from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.reactor import notify
from app.core.ids import NOTIFICATION_CHANNEL
from app.core.ids import new_uid
from app.db.models import NotificationChannel
from app.core.schemas import ListResponse
from app.reactor.rules import SEVERITY_ORDER
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import NotificationTestResult
from .schemas import NotificationChannelSchema
from .schemas import NotificationChannelCreateRequest
from .schemas import NotificationChannelUpdateRequest
from .repository import NotificationChannelRepository


def _hint(url: str | None) -> str | None:
    """Enough of a webhook to recognise, not enough to use.

    The host says which service it is and the last four characters distinguish two
    webhooks into different Slack channels. The path in between is the secret and
    never leaves the database.
    """
    if not url:
        return None
    host = urlparse(url).netloc or "?"
    return f"{host}/…{url[-4:]}"


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise Invalid("webhook_url must be an https URL", {"scheme": parsed.scheme})


class NotificationChannelService:
    """Declared configuration, so ordinary CRUD — with one asymmetry.

    `webhook_url` goes in and never comes out. Every other module's update is a
    read-modify-write over fields the caller just read back; this one cannot be,
    because the caller has never seen the current value and must not. An update that
    omits `webhook_url` therefore leaves the stored one alone rather than clearing it.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = NotificationChannelRepository(db)

    def schema(self, channel: NotificationChannel) -> NotificationChannelSchema:
        return NotificationChannelSchema(
            id=channel.uid,
            name=channel.name,
            kind=channel.kind,
            enabled=channel.enabled,
            min_severity=channel.min_severity,
            webhook_hint=_hint((channel.config or {}).get("webhook_url")),
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )

    async def get(self, ref: str) -> NotificationChannelSchema:
        channel = await self.repo.get(ref)
        if channel is None:
            raise NotFound(f"no notification channel {ref!r}")
        return self.schema(channel)

    async def list(
        self, limit: int | None = None, cursor: str | None = None, kind: str | None = None
    ) -> ListResponse[NotificationChannelSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(limit=size, after_seq=decode_cursor(cursor), kind=kind)
        kept, next_cursor = page(rows, size, lambda c: c.seq)
        return ListResponse(items=[self.schema(c) for c in kept], count=len(kept), next_cursor=next_cursor)

    async def create(self, data: NotificationChannelCreateRequest) -> NotificationChannelSchema:
        if data.kind not in notify.KINDS:
            raise Invalid(f"unknown channel kind {data.kind!r}", {"allowed": sorted(notify.KINDS)})
        if data.min_severity not in SEVERITY_ORDER:
            raise Invalid(f"unknown severity {data.min_severity!r}", {"allowed": sorted(SEVERITY_ORDER)})
        _check_url(data.webhook_url)
        channel = NotificationChannel(
            uid=new_uid(NOTIFICATION_CHANNEL),
            name=data.name,
            kind=data.kind,
            enabled=data.enabled,
            min_severity=data.min_severity,
            config={"webhook_url": data.webhook_url},
        )
        try:
            await self.repo.add(channel)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"notification channel {data.name!r} already exists") from exc
        return self.schema(channel)

    async def update(self, ref: str, data: NotificationChannelUpdateRequest) -> NotificationChannelSchema:
        channel = await self.repo.get(ref)
        if channel is None:
            raise NotFound(f"no notification channel {ref!r}")
        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        if "min_severity" in fields and fields["min_severity"] not in SEVERITY_ORDER:
            raise Invalid(f"unknown severity {fields['min_severity']!r}", {"allowed": sorted(SEVERITY_ORDER)})
        if "webhook_url" in fields:
            _check_url(fields["webhook_url"])
            channel.config = {**(channel.config or {}), "webhook_url": fields.pop("webhook_url")}
        for key, value in fields.items():
            setattr(channel, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("notification channel conflicts with an existing one") from exc
        return self.schema(channel)

    async def delete(self, ref: str) -> None:
        channel = await self.repo.get(ref)
        if channel is None:
            raise NotFound(f"no notification channel {ref!r}")
        await self.repo.delete(channel)
        await self.db.commit()

    async def test(self, ref: str) -> NotificationTestResult:
        """Posts a sample alert to the channel, in the same shape a real alert takes."""
        channel = await self.repo.get(ref)
        if channel is None:
            raise NotFound(f"no notification channel {ref!r}")
        message = notify.Message(
            severity="info",
            state="firing",
            title=f"test notification from Hangar ({settings.APP_ENV})",
            rule="channel.test",
            cluster=None,
            subject=None,
            details={"sent_at": dt.datetime.now(dt.UTC).isoformat()},
        )
        try:
            await notify.deliver(channel, message, settings.REACTOR_NOTIFY_TIMEOUT)
        except notify.DeliveryFailed as error:
            return NotificationTestResult(delivered=False, detail=str(error))
        return NotificationTestResult(delivered=True)
