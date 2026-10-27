"""Usage for many artifacts in one read.

`GET /v1/artifacts/{ref}/usage` answers for one artifact, and a list view that
shows a usage column would ask it once per row. This route answers the same
question for up to a page of artifacts in three statements, whatever the page
size: one to resolve the ids, one grouped count of workloads and one of derived
artifacts.
"""

from uuid import UUID
from typing import Annotated

from litestar import Response
from litestar import Controller
from litestar import get
from sqlalchemy import or_
from sqlalchemy import any_
from sqlalchemy import func
from sqlalchemy import select
from litestar.params import Parameter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Artifact
from app.db.models import Workload
from app.core.context import RequestContext
from app.core.schemas import Struct
from app.core.exceptions import Invalid
from app.core.pagination import MAX_LIMIT

from .schemas import ArtifactUsageSchema


class ArtifactUsageBatchSchema(Struct):
    """One entry per known id, in the order asked. Not a page of a collection, so
    there is no cursor or count beside `items`."""

    items: list[ArtifactUsageSchema]


class ArtifactUsageReader:
    """Counts what depends on each of a set of artifacts, with the single-artifact
    route's definitions: a workload counts once whether it serves the weights or
    names them as an adapter, and downstream is the artifacts naming one as base."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def usage(self, uids: list[str]) -> list[ArtifactUsageSchema]:
        """One schema per known uid, in the order asked. Unknown uids are left out."""
        rows = await self.db.execute(select(Artifact.id, Artifact.uid).where(Artifact.uid.in_(uids)))
        by_uid: dict[str, UUID] = {row.uid: row.id for row in rows}
        if not by_uid:
            return []
        ids = list(by_uid.values())

        workloads = await self.db.execute(
            select(Artifact.id, func.count(func.distinct(Workload.id)).label("count"))
            .select_from(Artifact)
            .join(
                Workload,
                or_(Workload.artifact_id == Artifact.id, Artifact.id == any_(Workload.adapter_artifact_ids)),
            )
            .where(Artifact.id.in_(ids))
            .group_by(Artifact.id)
        )
        workload_counts = {row.id: int(row.count) for row in workloads}

        downstream = await self.db.execute(
            select(Artifact.base_artifact_id, func.count(Artifact.id).label("count"))
            .where(Artifact.base_artifact_id.in_(ids))
            .group_by(Artifact.base_artifact_id)
        )
        downstream_counts = {row.base_artifact_id: int(row.count) for row in downstream}

        return [
            ArtifactUsageSchema(
                artifact_id=uid,
                workload_count=workload_counts.get(by_uid[uid], 0),
                downstream_count=downstream_counts.get(by_uid[uid], 0),
            )
            for uid in uids
            if uid in by_uid
        ]


def requested_uids(values: list[str]) -> list[str]:
    """The `id` parameters, deduplicated in order. Names are refused: a name is not
    unique among artifacts, and a batch read that resolved each to its oldest match
    would answer for artifacts the caller did not mean."""
    uids = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not uids:
        raise Invalid("name at least one artifact with ?id=")
    if len(uids) > MAX_LIMIT:
        raise Invalid(f"at most {MAX_LIMIT} artifacts per read", {"count": len(uids), "max": MAX_LIMIT})
    names = [uid for uid in uids if not is_uid(uid)]
    if names:
        raise Invalid("ids only; a name can match more than one artifact", {"not_ids": names})
    return uids


class ArtifactUsageRoutes(Controller):
    """Its own controller on the artifacts path, so `/artifacts/usage` sits beside
    `/artifacts/{ref}` the way `/artifacts/resolve` does: a literal segment wins
    over the ref, and an artifact named `usage` is reached by its id."""

    tags = ["Artifacts"]
    path = "/artifacts/usage"

    @get()
    async def list_artifact_usage(
        self,
        context: RequestContext,
        ids: Annotated[list[str], Parameter(query="id")],
    ) -> Response[ArtifactUsageBatchSchema]:
        """Workload and downstream counts for each artifact named by `?id=`, up to 200.

        An unknown id is left out of `items` and does not fail the read, so a list
        page that raced a delete still renders.
        """
        return Response(
            ArtifactUsageBatchSchema(items=await ArtifactUsageReader(context.db).usage(requested_uids(ids)))
        )
