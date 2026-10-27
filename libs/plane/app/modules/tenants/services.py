import datetime as dt

from sqlalchemy import Row
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthManager
from app.config import settings
from app.core.ids import TENANT
from app.core.ids import TENANT_TOKEN
from app.core.ids import TENANT_API_KEY
from app.core.ids import new_uid
from app.db.models import Tenant
from app.db.models import TenantToken
from app.db.models import TenantAPIKey
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import TenantSchema
from .schemas import TenantTokenSchema
from .schemas import TenantAPIKeySchema
from .schemas import TenantCreateRequest
from .schemas import TenantUpdateRequest
from .schemas import TenantTokenCreateRequest
from .schemas import TenantAPIKeyCreateRequest
from .repository import TenantRepository


def _revision(tenant: Tenant) -> str:
    stamp = tenant.updated_at or tenant.created_at
    return str(int(stamp.timestamp() * 1_000_000))


INGRESS_VALUES = ("internal", "public")
AUTH_VALUES = ("none", "api_key")

RESERVED_SLUGS = frozenset({"admin"})
"""`admin` is the slug the login form reads as the fleet operator, so a tenant
holding it could never sign in as itself."""


def needs_key(ingress: str, auth: str, host: str | None) -> bool:
    """Whether the public listener must check a key for this tenant.

    True for every `api_key` tenant, and for an `auth: none` tenant the public
    listener can reach (a host, or `ingress: public`) unless the plane's
    operator opted in with `TENANT_ALLOW_PUBLIC_NO_AUTH`. The gateway feed
    sends `auth: api_key` for the second kind, so a row stored before the
    create/update check existed is held to a key too, not grandfathered.
    """
    if auth == "api_key":
        return True
    public = ingress == "public" or bool(host)
    return public and not settings.TENANT_ALLOW_PUBLIC_NO_AUTH


def _check_ingress_auth(ingress: str, auth: str, host: str | None) -> None:
    """Refuses a tenant the public listener would serve with no key check.

    The gateway's public listener finds a tenant by its `host`, whatever
    `ingress` says, and checks a key only when `auth` is `api_key`. So a tenant
    with a host, or one declared `public`, and `auth: none` is inference for
    anyone who finds the name. That is refused unless the plane's operator has
    opted in with `TENANT_ALLOW_PUBLIC_NO_AUTH`. `none` on an internal tenant
    with no host is fine: there the listener itself is the proof (ADR 0016).
    """
    if ingress not in INGRESS_VALUES:
        raise Invalid(f"ingress must be one of {INGRESS_VALUES}, got {ingress!r}")
    if auth not in AUTH_VALUES:
        raise Invalid(f"auth must be one of {AUTH_VALUES}, got {auth!r}")
    if auth == "none" and needs_key(ingress, auth, host):
        raise Invalid(
            "a tenant reachable on the public listener needs auth 'api_key'",
            {"ingress": ingress, "host": host, "setting": "HANGAR_TENANT_ALLOW_PUBLIC_NO_AUTH"},
        )


class TenantService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = TenantRepository(db)

    def schema(self, tenant: Tenant, project_count: int) -> TenantSchema:
        return TenantSchema(
            id=tenant.uid,
            slug=tenant.slug,
            name=tenant.name,
            enabled=tenant.enabled,
            ingress=tenant.ingress,
            auth=tenant.auth,
            host=tenant.host,
            project_count=project_count,
            created_at=tenant.created_at,
            updated_at=tenant.updated_at,
            revision=_revision(tenant),
        )

    def _row(self, row: Row) -> TenantSchema:
        return self.schema(row[0], row.project_count)

    async def _one(self, ref: str, lock: bool = False) -> Row:
        row = await self.repo.get(ref, lock=lock)
        if row is None:
            raise NotFound(f"no tenant {ref!r}")
        return row

    async def get(self, ref: str) -> TenantSchema:
        return self._row(await self._one(ref))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> ListResponse[TenantSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            enabled=enabled,
            search=search,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self._row(r) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: TenantCreateRequest) -> TenantSchema:
        if not data.slug:
            raise Invalid("slug is required")
        if not data.name:
            raise Invalid("name is required")
        if data.slug in RESERVED_SLUGS:
            raise Invalid(f"{data.slug!r} is a reserved slug", {"reserved": sorted(RESERVED_SLUGS)})
        _check_ingress_auth(data.ingress, data.auth, data.host)
        tenant = Tenant(
            uid=new_uid(TENANT),
            slug=data.slug,
            name=data.name,
            enabled=data.enabled,
            ingress=data.ingress,
            auth=data.auth,
            host=data.host,
        )
        try:
            await self.repo.add(tenant)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"tenant {data.slug!r} already exists") from exc
        return await self.get(tenant.uid)

    async def update(self, ref: str, data: TenantUpdateRequest, if_match: str | None = None) -> TenantSchema:
        row = await self._one(ref, lock=True)
        tenant: Tenant = row[0]
        if if_match is not None and if_match != _revision(tenant):
            raise Conflict("tenant changed since it was read", {"revision": _revision(tenant)})

        fields = data.to_dict(exclude_unset=True)
        trust = {key: fields.get(key, getattr(tenant, key)) for key in ("ingress", "auth", "host")}
        # Checked only when the patch changes how the tenant is trusted, so a tenant
        # left public with no key from before this rule can still be renamed or
        # switched off. It does not serve keyless meanwhile: the gateway feed
        # reports it as `api_key` (see `needs_key`).
        if any(trust[key] != getattr(tenant, key) for key in trust):
            _check_ingress_auth(trust["ingress"], trust["auth"], trust["host"])
        for field, value in fields.items():
            setattr(tenant, field, value)

        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"host {fields.get('host')!r} is already in use") from exc
        await self.db.commit()
        return await self.get(tenant.uid)

    async def delete(self, ref: str) -> None:
        """Refuses while projects still hang off it.

        The foreign key cascades, so deleting a tenant with projects would take
        their quotas and allocations with it without anyone having asked. The
        refusal names the count, and emptying the tenant is the caller's decision
        to make one project at a time.
        """
        row = await self._one(ref)
        if row.project_count:
            raise Conflict(
                f"tenant {ref!r} still holds {row.project_count} project(s)",
                {"project_count": row.project_count},
            )
        await self.repo.delete(row[0])
        await self.db.commit()

    def _token(self, token: TenantToken, slug: str, plaintext: str | None = None) -> TenantTokenSchema:
        return TenantTokenSchema(
            id=token.uid,
            tenant=slug,
            name=token.name,
            token=plaintext,
            last_used_at=token.last_used_at,
            revoked_at=token.revoked_at,
            created_at=token.created_at,
        )

    async def list_tokens(self, ref: str) -> "list[TenantTokenSchema]":
        """Every credential ever minted for this tenant, revoked ones included.

        Revoked rows stay in the answer because the question somebody asks here is
        not "what works" but "what was ever handed out", and a credential that
        disappears when it is revoked cannot be told from one that never existed.
        """
        row = await self._one(ref)
        tenant: Tenant = row[0]
        result = await self.db.execute(
            select(TenantToken).where(TenantToken.tenant_id == tenant.id).order_by(TenantToken.seq.asc())
        )
        return [self._token(token, tenant.slug) for token in result.scalars().all()]

    async def mint_token(self, ref: str, data: TenantTokenCreateRequest) -> TenantTokenSchema:
        """Mints a console credential for one tenant and returns it once.

        The plaintext is on this response and nowhere else, ever. Minting a second
        one does not invalidate the first: rotation is issue, move the reader,
        revoke: three steps, none of which is a coordinated flip.
        """
        row = await self._one(ref)
        tenant: Tenant = row[0]
        plaintext, token_hash = AuthManager.mint_tenant_token()
        token = TenantToken(
            uid=new_uid(TENANT_TOKEN),
            tenant_id=tenant.id,
            token_hash=token_hash,
            name=data.name or "console",
        )
        self.db.add(token)
        await self.db.flush()
        await self.db.commit()
        return self._token(token, tenant.slug, plaintext=plaintext)

    async def revoke_token(self, ref: str, token_ref: str) -> None:
        """Revokes one credential. Idempotent on an already-revoked one.

        A revoked token also ends the sessions it opened, at their next refresh:
        `AuthService.rotate_refresh_token` re-checks the credential instead of
        trusting the session it minted.
        """
        row = await self._one(ref)
        tenant: Tenant = row[0]
        result = await self.db.execute(
            select(TenantToken).where(
                TenantToken.tenant_id == tenant.id,
                TenantToken.uid == token_ref,
            )
        )
        token = result.scalars().first()
        if token is None:
            raise NotFound(f"no token {token_ref!r} on tenant {ref!r}")
        if token.revoked_at is None:
            token.revoked_at = dt.datetime.now(dt.UTC)
            await self.db.commit()

    def _key(self, key: TenantAPIKey, slug: str, plaintext: str | None = None) -> TenantAPIKeySchema:
        return TenantAPIKeySchema(
            id=key.uid,
            tenant=slug,
            name=key.name,
            key=plaintext,
            last_used_at=key.last_used_at,
            revoked_at=key.revoked_at,
            created_at=key.created_at,
        )

    async def list_keys(self, ref: str) -> "list[TenantAPIKeySchema]":
        """Every key ever minted for this tenant, revoked ones included: same
        reasoning as `list_tokens`."""
        row = await self._one(ref)
        tenant: Tenant = row[0]
        result = await self.db.execute(
            select(TenantAPIKey).where(TenantAPIKey.tenant_id == tenant.id).order_by(TenantAPIKey.seq.asc())
        )
        return [self._key(key, tenant.slug) for key in result.scalars().all()]

    async def mint_key(self, ref: str, data: TenantAPIKeyCreateRequest) -> TenantAPIKeySchema:
        """Mints a gateway inference key for one tenant and returns it once.

        Operator-only, same as `mint_token`: a tenant does not mint its own
        inference credentials through this route, an operator hands them out.
        """
        row = await self._one(ref)
        tenant: Tenant = row[0]
        plaintext, key_hash = AuthManager.mint_tenant_api_key()
        key = TenantAPIKey(
            uid=new_uid(TENANT_API_KEY),
            tenant_id=tenant.id,
            key_hash=key_hash,
            name=data.name or "default",
        )
        self.db.add(key)
        await self.db.flush()
        await self.db.commit()
        return self._key(key, tenant.slug, plaintext=plaintext)

    async def revoke_key(self, ref: str, key_ref: str) -> None:
        """Revokes one key. Idempotent on an already-revoked one."""
        row = await self._one(ref)
        tenant: Tenant = row[0]
        result = await self.db.execute(
            select(TenantAPIKey).where(
                TenantAPIKey.tenant_id == tenant.id,
                TenantAPIKey.uid == key_ref,
            )
        )
        key = result.scalars().first()
        if key is None:
            raise NotFound(f"no key {key_ref!r} on tenant {ref!r}")
        if key.revoked_at is None:
            key.revoked_at = dt.datetime.now(dt.UTC)
            await self.db.commit()
