"""`GET /v1/changes`: the change log as ordered hints (ADR 0035).

A stub for the watch that P1.10 streams: changes after `since`, oldest first, each a hint naming
the object, never its body. A `since` the log has compacted past is 410 Gone; relist and resume
from the list's `resourceVersion`.
"""

from typing import Annotated

from msgspec import Meta
from litestar import Controller
from litestar import get
from sqlalchemy import select
from litestar.params import Parameter

from app.db import changes
from app.db.models import Project
from app.core.context import RequestContext
from app.core.schemas import Struct


class ChangeSchema(Struct, kw_only=True):
    resource_version: str
    kind: str
    namespace: str | None
    name: str
    uid: str
    op: str
    generation: int | None


class ChangeListSchema(Struct, kw_only=True):
    items: list[ChangeSchema]
    resource_version: str
    """Resume from here: the last item's seq, or `since` when nothing is newer."""
    compacted_through: str


class ChangeRoutes(Controller):
    tags = ["Changes"]
    path = "/changes"

    @get("")
    async def list_changes(
        self,
        context: RequestContext,
        since: Annotated[int, Meta(ge=0)] = Parameter(query="since", default=0),
        limit: Annotated[int, Meta(ge=1, le=1000)] = Parameter(query="limit", default=500),
    ) -> ChangeListSchema:
        rows, through = await changes.since(context.db, since, limit)
        ids = {r.project_id for r in rows if r.project_id is not None}
        slugs = (
            dict((await context.db.execute(select(Project.id, Project.slug).where(Project.id.in_(ids)))).tuples().all())
            if ids
            else {}
        )
        return ChangeListSchema(
            items=[
                ChangeSchema(
                    resource_version=str(r.seq),
                    kind=r.kind,
                    namespace=slugs.get(r.project_id),
                    name=r.name,
                    uid=r.uid,
                    op=r.op,
                    generation=r.generation,
                )
                for r in rows
            ],
            resource_version=str(rows[-1].seq if rows else since),
            compacted_through=str(through),
        )
