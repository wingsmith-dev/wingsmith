from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import TENANT
from app.core.ids import new_uid
from app.db.models import Tenant
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import TenantSchema
from .schemas import TenantCreateRequest
from .schemas import TenantUpdateRequest
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
