from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tenant
from app.db.models import Project
from app.core.filters import ref_ids


def tenant_ids(ref: str) -> Select:
    """A tenant is referenced by its `ten_` id or by its slug."""
    return ref_ids(Tenant, ref, name_column="slug")


class TenantRepository:
    """Reads tenants with the number of projects beneath each one counted in.

    The count is a correlated subquery rather than a join and a group-by: it is
    one number per row, and grouping would put every selected column into the
    clause for no gain.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        projects = (
            select(func.count(Project.id)).where(Project.tenant_id == Tenant.id).correlate(Tenant).scalar_subquery()
        )
        return select(Tenant, projects.label("project_count"))

    def get_query(self, ref: str) -> Select:
        return self._base().where(Tenant.id.in_(tenant_ids(ref)))

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> Select:
        query = self._base().order_by(Tenant.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Tenant.seq > after_seq)
        if enabled is not None:
            query = query.where(Tenant.enabled.is_(enabled))
        if search is not None:
            query = query.where(Tenant.name.ilike(f"%{search}%"))
        return query

    async def get(self, ref: str) -> Row | None:
        result = await self.db.execute(self.get_query(ref).limit(1))
        return result.first()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def add(self, tenant: Tenant) -> Tenant:
        self.db.add(tenant)
        await self.db.flush()
        return tenant

    async def delete(self, tenant: Tenant) -> None:
        await self.db.delete(tenant)
        await self.db.flush()
