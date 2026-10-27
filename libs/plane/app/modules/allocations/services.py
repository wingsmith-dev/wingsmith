from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Allocation
from app.core.schemas import ListResponse
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import AllocationSchema
from .schemas import AllocationDeviceSchema
from .repository import AllocationRepository


def gang_complete(allocation: Allocation, live_devices: int) -> bool:
    """Whether the allocation currently holds every device it was granted.

    A gang takes all N devices or none, so a live allocation short of its own
    `device_count` is a partially satisfied gang and not merely a smaller one.
    A released allocation holds nothing by definition and is not incomplete.
    """
    if allocation.released_at is not None:
        return False
    return live_devices >= allocation.device_count


class AllocationService:
    """Observed, read-only, and read-only by construction.

    There is no create, update or delete method on this class: not methods that
    refuse, but methods that do not exist. Allocation is decided by placement and
    reported by the cluster, and ingestion is its only writer. Releasing capacity
    is a consequence of a workload ending, never a `DELETE` on this resource.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = AllocationRepository(db)

    def member(self, row: Row) -> AllocationDeviceSchema:
        member = row[0]
        return AllocationDeviceSchema(
            device=row.device_uid,
            device_uuid=row.device_uuid,
            node=row.node_uid,
            live=member.released_at is None,
            released_at=member.released_at,
        )

    def schema(self, row: Row, devices: list[AllocationDeviceSchema]) -> AllocationSchema:
        allocation: Allocation = row[0]
        live = allocation.released_at is None
        return AllocationSchema(
            id=allocation.uid,
            cluster=row.cluster_uid,
            cluster_name=row.cluster_name,
            project=row.project_uid,
            project_slug=row.project_slug,
            workload=row.workload_uid,
            replica=row.replica_uid,
            device_class=allocation.device_class,
            device_count=allocation.device_count,
            over_quota=allocation.over_quota,
            phase=allocation.phase,
            live=live,
            gang_complete=gang_complete(allocation, sum(1 for d in devices if d.live)),
            acquired_at=allocation.acquired_at,
            released_at=allocation.released_at,
            observed_at=allocation.observed_at,
            created_at=allocation.created_at,
            updated_at=allocation.updated_at,
            devices=devices,
        )

    async def _hydrate(self, rows: list[Row]) -> list[AllocationSchema]:
        grouped = await self.repo.members([r[0].id for r in rows])
        return [self.schema(r, [self.member(m) for m in grouped.get(r[0].id, [])]) for r in rows]

    async def get(self, ref: str) -> AllocationSchema:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no allocation {ref!r}")
        return (await self._hydrate([row]))[0]

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        project: str | None = None,
        device_class: str | None = None,
        phase: str | None = None,
        over_quota: bool | None = None,
        live: bool | None = None,
        replica: str | None = None,
        device: str | None = None,
    ) -> ListResponse[AllocationSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            cluster=cluster,
            project=project,
            device_class=device_class,
            phase=phase,
            over_quota=over_quota,
            live=live,
            replica=replica,
            device=device,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = await self._hydrate(kept)
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)
