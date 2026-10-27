from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import PROJECT
from app.core.ids import new_uid
from app.db.models import Tenant
from app.db.models import Project
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import ProjectSchema
from .schemas import ProjectTenantSchema
from .schemas import ProjectCreateRequest
from .schemas import ProjectUpdateRequest
from .repository import ProjectRepository


def _revision(project: Project) -> str:
    stamp = project.updated_at or project.created_at
    return str(int(stamp.timestamp() * 1_000_000))


class ProjectService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ProjectRepository(db)

    def schema(self, project: Project, tenant: Tenant, priority_class: str | None) -> ProjectSchema:
        return ProjectSchema(
            id=project.uid,
            tenant=ProjectTenantSchema(
                id=tenant.uid,
                slug=tenant.slug,
                name=tenant.name,
                enabled=tenant.enabled,
            ),
            slug=project.slug,
            name=project.name,
            default_priority_class=priority_class,
            enabled=project.enabled,
            effective_enabled=bool(project.enabled and tenant.enabled),
            created_at=project.created_at,
            updated_at=project.updated_at,
            revision=_revision(project),
        )

    def _row(self, row: Row) -> ProjectSchema:
        return self.schema(row[0], row[1], row.default_priority_class)

    async def _one(self, ref: str, tenant: str | None = None, lock: bool = False) -> Row:
        row = await self.repo.get(ref, tenant, lock=lock)
        if row is None:
            raise NotFound(f"no project {ref!r}")
        return row

    async def get(self, ref: str, tenant: str | None = None) -> ProjectSchema:
        return self._row(await self._one(ref, tenant))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        tenant: str | None = None,
        enabled: bool | None = None,
        search: str | None = None,
    ) -> ListResponse[ProjectSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            tenant=tenant,
            enabled=enabled,
            search=search,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self._row(r) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: ProjectCreateRequest) -> ProjectSchema:
        if not data.slug:
            raise Invalid("slug is required")
        tenant = await self.repo.tenant_by_ref(data.tenant)
        if tenant is None:
            raise NotFound(f"no tenant {data.tenant!r}")
        priority_class = None
        if data.default_priority_class is not None:
            priority_class = await self.repo.priority_class_by_ref(data.default_priority_class)
            if priority_class is None:
                raise NotFound(f"no priority class {data.default_priority_class!r}")
        project = Project(
            uid=new_uid(PROJECT),
            tenant_id=tenant.id,
            slug=data.slug,
            name=data.name,
            default_priority_class_id=priority_class.id if priority_class else None,
            enabled=data.enabled,
        )
        try:
            await self.repo.add(project)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"project {data.slug!r} already exists in {data.tenant!r}") from exc
        return await self.get(project.uid)

    async def update(
        self, ref: str, data: ProjectUpdateRequest, if_match: str | None = None, tenant: str | None = None
    ) -> ProjectSchema:
        row = await self._one(ref, tenant, lock=True)
        project: Project = row[0]
        if if_match is not None and if_match != _revision(project):
            raise Conflict("project changed since it was read", {"revision": _revision(project)})

        fields = data.to_dict(exclude_unset=True)
        if "default_priority_class" in fields:
            ref_value = fields.pop("default_priority_class")
            if ref_value is None:
                project.default_priority_class_id = None
            else:
                priority_class = await self.repo.priority_class_by_ref(ref_value)
                if priority_class is None:
                    raise NotFound(f"no priority class {ref_value!r}")
                project.default_priority_class_id = priority_class.id
        for key, value in fields.items():
            setattr(project, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("project conflicts with an existing one") from exc
        return await self.get(project.uid)

    async def delete(self, ref: str, tenant: str | None = None) -> None:
        """A project holding workloads or allocations refuses to go.

        Both foreign keys are `ON DELETE RESTRICT`, so the database is the one
        saying no; this turns that into the contract's `409 conflict` instead of
        letting an integrity error surface as a 500.
        """
        row = await self._one(ref, tenant)
        try:
            await self.repo.delete(row[0])
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"project {ref!r} still owns workloads or allocations") from exc
