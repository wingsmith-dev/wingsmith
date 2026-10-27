from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node
from app.core.schemas import ListResponse
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import NodeSchema
from .schemas import NodesSummarySchema
from .schemas import NodeConditionSchema
from .schemas import NodeDeviceRollupSchema
from .repository import NodeRepository


class NodeService:
    """Read-only, by construction, not by refusal.

    Nodes are observed: they are written by ingestion and discovered by the
    operator, so there is no create, update or delete here at all. Methods that
    exist only to raise would still be an API surface, and `POST /v1/nodes` is a
    category error instead of a permission failure.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = NodeRepository(db)

    def schema(
        self,
        row: Row,
        rollup: dict[str, int] | None = None,
        allocated: int = 0,
        device_class: str | None = None,
    ) -> NodeSchema:
        node: Node = row[0]
        counts = rollup or {}
        return NodeSchema(
            id=node.uid,
            name=node.name,
            cluster=row[1],
            cluster_name=row[2],
            pool=row[3],
            pool_name=row[4],
            condition=node.condition,
            conditions=[NodeConditionSchema(**c) for c in (node.conditions or [])],
            cordoned=node.cordoned,
            instance_type=node.instance_type,
            cloud=node.cloud,
            zone=node.zone,
            region=node.region,
            ready=node.ready,
            cpu_cores=node.cpu_cores,
            memory_mib=node.memory_mib,
            cpu_arch=node.cpu_arch,
            os_image=node.os_image,
            kernel_version=node.kernel_version,
            driver_version=node.driver_version,
            operator_version=node.operator_version,
            labels=node.labels,
            taints=node.taints,
            devices=NodeDeviceRollupSchema(
                total=sum(counts.values()),
                by_health=counts,
                device_class=device_class,
                allocated=allocated,
            ),
            joined_at=node.joined_at,
            observed_at=node.observed_at,
            created_at=node.created_at,
            updated_at=node.updated_at,
        )

    async def get(self, ref: str, cluster: str | None = None) -> NodeSchema:
        row = await self.repo.get(ref, cluster)
        if row is None:
            raise NotFound(f"no node {ref!r}")
        node_id = row[0].id
        rollup = await self.repo.device_rollup([node_id])
        allocated = await self.repo.allocated_rollup([node_id])
        classes = await self.repo.class_rollup([node_id])
        return self.schema(row, rollup.get(node_id), allocated.get(node_id, 0), classes.get(node_id))

    async def summary(self) -> NodesSummarySchema:
        total, ready, cordoned, faulted_devices = await self.repo.summary()
        return NodesSummarySchema(total=total, ready=ready, cordoned=cordoned, faulted_devices=faulted_devices)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        pool: str | None = None,
        condition: str | None = None,
        ready: bool | None = None,
        cordoned: bool | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[NodeSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            cluster=cluster,
            pool=pool,
            condition=condition,
            ready=ready,
            cordoned=cordoned,
            search=search,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        node_ids = [r[0].id for r in kept]
        rollup = await self.repo.device_rollup(node_ids)
        allocated = await self.repo.allocated_rollup(node_ids)
        classes = await self.repo.class_rollup(node_ids)
        items = [self.schema(r, rollup.get(r[0].id), allocated.get(r[0].id, 0), classes.get(r[0].id)) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)
