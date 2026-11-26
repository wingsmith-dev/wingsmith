import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project
from app.db.models import Credential
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor
from app.modules.projects.repository import ProjectRepository

from .scope import PROJECT
from .schemas import NAME
from .schemas import CredentialSchema
from .schemas import CredentialPutRequest


class CredentialService:
    """A project's credentials. Admin only: a tenant credential reaches none of these routes."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _project(self, ref: str, tenant: str | None) -> Project:
        row = await ProjectRepository(self.db).get(ref, tenant)
        if row is None:
            raise NotFound(f"no project {ref!r}")
        return row[0]

    @staticmethod
    def schema(row: Credential, project: Project) -> CredentialSchema:
        return CredentialSchema(
            name=row.name,
            project=project.uid,
            kind=row.kind,
            secret_ref=row.secret_ref,
            expires_at=row.expires_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _query(self, project: Project):
        return select(Credential).where(Credential.scope_type == PROJECT, Credential.scope_id == project.id)

    async def list(
        self, ref: str, tenant: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> ListResponse[CredentialSchema]:
        project = await self._project(ref, tenant)
        size = clamp_limit(limit)
        query = self._query(project).order_by(Credential.seq).limit(size + 1)
        if (after := decode_cursor(cursor)) is not None:
            query = query.where(Credential.seq > after)
        rows = list((await self.db.execute(query)).scalars())
        kept, next_cursor = page(rows, size, lambda c: c.seq)
        items = [self.schema(row, project) for row in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def put(
        self, ref: str, name: str, data: CredentialPutRequest, tenant: str | None = None
    ) -> tuple[CredentialSchema, bool]:
        """Creates or replaces the named credential; the bool says whether it was created."""
        if not re.match(NAME, name):
            raise Invalid(f"credential name {name!r} must be a lowercase DNS label")
        project = await self._project(ref, tenant)
        result = await self.db.execute(self._query(project).where(Credential.name == name).with_for_update())
        row = result.scalar_one_or_none()
        created = row is None
        if row is None:
            row = Credential(name=name, scope_type=PROJECT, scope_id=project.id)
            self.db.add(row)
        row.kind = data.kind
        row.secret_ref = data.secret_ref
        row.expires_at = data.expires_at
        row.revoked_at = None
        await self.db.commit()
        await self.db.refresh(row)
        return self.schema(row, project), created

    async def delete(self, ref: str, name: str, tenant: str | None = None) -> None:
        project = await self._project(ref, tenant)
        result = await self.db.execute(self._query(project).where(Credential.name == name))
        row = result.scalar_one_or_none()
        if row is None:
            raise NotFound(f"no credential {name!r} in project {ref!r}")
        await self.db.delete(row)
        await self.db.commit()
