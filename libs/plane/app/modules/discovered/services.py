import datetime as dt

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DiscoveredWorkload
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import DecisionRequest
from .schemas import DiscoveredWorkloadSchema
from .repository import DiscoveredWorkloadRepository


TRACKED = "tracked"
IGNORED = "ignored"
NEW = "new"

STATUSES = frozenset({NEW, TRACKED, IGNORED})


class DiscoveredWorkloadService:
    """Sightings in, decisions out.

    The service has no method that declares anything. Tracking a discovered
    workload writes one row in `demand_workload_adoption` and stops there: it
    creates no `Deployment`, resolves no recipe, and produces nothing that
    `stream_desired` will ever read. That is deliberate and is the reason this
    module exists rather than an "import to deployment" endpoint — the plane
    cannot cause a rollout of a workload it has declared nothing about, and a
    model whose weights are already resident stays resident.
    """

    def __init__(self, db: AsyncSession, principal: str | None = None) -> None:
        self.db = db
        self.principal = principal
        self.repo = DiscoveredWorkloadRepository(db)

    def schema(self, row: Row) -> DiscoveredWorkloadSchema:
        workload: DiscoveredWorkload = row[0]
        adoption = row[3]
        return DiscoveredWorkloadSchema(
            id=workload.uid,
            cluster_id=row[1],
            cluster_name=row[2],
            namespace=workload.namespace,
            name=workload.name,
            kind=workload.kind,
            model=workload.model,
            images=list(workload.images),
            replicas_desired=workload.replicas_desired,
            replicas_ready=workload.replicas_ready,
            devices_per_replica=workload.devices_per_replica,
            devices_total=workload.devices_total,
            node_ids=list(workload.node_uids),
            labels=workload.labels,
            status=adoption.decision if adoption is not None else NEW,
            project_id=row[4],
            note=adoption.note if adoption is not None else None,
            decided_by=adoption.decided_by if adoption is not None else None,
            decided_at=adoption.decided_at if adoption is not None else None,
            first_seen_at=workload.first_seen_at,
            observed_at=workload.observed_at,
        )

    async def _one(self, ref: str, cluster: str | None = None) -> Row:
        row = await self.repo.get(ref, cluster)
        if row is None:
            raise NotFound(f"no discovered workload {ref!r}")
        return row

    async def get(self, ref: str, cluster: str | None = None) -> DiscoveredWorkloadSchema:
        return self.schema(await self._one(ref, cluster))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> ListResponse[DiscoveredWorkloadSchema]:
        if status is not None and status not in STATUSES:
            raise Invalid(f"unknown status {status!r}", {"allowed": sorted(STATUSES)})
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            cluster=cluster,
            namespace=namespace,
            status=status,
            search=search,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(row) for row in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def decide(self, ref: str, decision: str, data: DecisionRequest) -> DiscoveredWorkloadSchema:
        """Records what we decided about a sighting and nothing else."""
        row = await self._one(ref)
        workload: DiscoveredWorkload = row[0]

        project_id = None
        if data.project is not None:
            found = await self.repo.project_by_ref(data.project)
            if found is None:
                raise NotFound(f"no project {data.project!r}")
            project_id = found[0]

        await self.repo.decide(
            {
                "cluster_id": workload.cluster_id,
                "workload_uid": workload.uid,
                "decision": decision,
                "project_id": project_id,
                "note": data.note,
                "decided_by": self.principal,
                "decided_at": dt.datetime.now(dt.UTC),
            }
        )
        await self.db.commit()
        return await self.get(workload.uid)
