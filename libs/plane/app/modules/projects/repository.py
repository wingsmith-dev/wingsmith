from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Tenant
from app.db.models import Project
from app.db.models import PriorityClass
from app.core.filters import ref_ids


def tenant_ids(ref: str) -> Select:
    """`Tenant` carries no `uid`, so its reference is its slug."""
    return ref_ids(Tenant, ref, name_column="slug")


def priority_class_ids(ref: str) -> Select:
    """`PriorityClass` carries no `uid` either: its reference is its name."""
    return ref_ids(PriorityClass, ref, name_column="name")


class ProjectRepository:
    """Reads projects with their tenant and default priority class joined in.

    Both relationships are `lazy="noload"` and both are on every response, so the
    join is explicit here rather than a per-row round trip in the serializer.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return (
            select(Project, Tenant, PriorityClass.name.label("default_priority_class"))
            .join(Tenant, Project.tenant_id == Tenant.id)
            .outerjoin(PriorityClass, Project.default_priority_class_id == PriorityClass.id)
        )

    def get_query(self, ref: str, tenant: str | None = None) -> Select:
        """A project slug is unique only inside its tenant, so `tenant` narrows it."""
        column = Project.uid if is_uid(ref) else Project.slug
        query = self._base().where(column == ref)
        if tenant is not None:
            query = query.where(Project.tenant_id.in_(tenant_ids(tenant)))
        return query

    def list_query(
        self,
        limit: int,
        after_seq: int | None = None,
        tenant: str | None = None,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> Select:
        query = self._base().order_by(Project.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Project.seq > after_seq)
        if tenant is not None:
            query = query.where(Project.tenant_id.in_(tenant_ids(tenant)))
        if enabled is not None:
            query = query.where(Project.enabled.is_(enabled))
        if search is not None:
            query = query.where(Project.name.ilike(f"%{search}%"))
        return query

    async def get(self, ref: str, tenant: str | None = None) -> Row | None:
        result = await self.db.execute(self.get_query(ref, tenant).limit(1))
        return result.first()

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.list_query(**kwargs))
        return list(result.all())

    async def tenant_by_ref(self, ref: str) -> Tenant | None:
        result = await self.db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids(ref))))
        return result.scalar_one_or_none()

    async def priority_class_by_ref(self, ref: str) -> PriorityClass | None:
        result = await self.db.execute(select(PriorityClass).where(PriorityClass.id.in_(priority_class_ids(ref))))
        return result.scalar_one_or_none()

    async def add(self, project: Project) -> Project:
        self.db.add(project)
        await self.db.flush()
        return project

    async def delete(self, project: Project) -> None:
        await self.db.delete(project)
        await self.db.flush()
