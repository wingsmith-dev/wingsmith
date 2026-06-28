from datetime import datetime
from datetime import timezone

from sqlalchemy import Row
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthManager
from app.core.ids import TENANT
from app.core.ids import TENANT_TOKEN
from app.core.ids import new_uid
from app.db.models import Tenant
from app.db.models import TenantToken
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import TenantSchema
from .schemas import TenantTokenSchema
from .schemas import TenantCreateRequest
from .schemas import TenantUpdateRequest
from .schemas import TenantTokenCreateRequest
from .repository import TenantRepository


def _revision(tenant: Tenant) -> str:
    stamp = tenant.updated_at or tenant.created_at
    return str(int(stamp.timestamp() * 1_000_000))


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
            project_count=project_count,
            created_at=tenant.created_at,
            updated_at=tenant.updated_at,
            revision=_revision(tenant),
        )

    def _row(self, row: Row) -> TenantSchema:
        return self.schema(row[0], row.project_count)

    async def _one(self, ref: str) -> Row:
        row = await self.repo.get(ref)
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
        tenant = Tenant(uid=new_uid(TENANT), slug=data.slug, name=data.name, enabled=data.enabled)
        try:
            await self.repo.add(tenant)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"tenant {data.slug!r} already exists") from exc
        return await self.get(tenant.uid)

    async def update(self, ref: str, data: TenantUpdateRequest, if_match: str | None = None) -> TenantSchema:
        row = await self._one(ref)
        tenant: Tenant = row[0]
        if if_match is not None and if_match != _revision(tenant):
            raise Conflict("tenant changed since it was read", {"revision": _revision(tenant)})

        for field, value in data.to_dict(exclude_unset=True).items():
            setattr(tenant, field, value)

        await self.db.flush()
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
        not "what works" but "what was ever handed out" — and a credential that
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
        revoke — three steps, none of which is a coordinated flip.
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

        A revoked token also ends the sessions it opened, at their next refresh —
        `AuthService.rotate_refresh_token` re-checks the credential rather than
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
            token.revoked_at = datetime.now(tz=timezone.utc)
            await self.db.commit()
