from msgspec import UNSET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import POOL
from app.core.ids import new_uid
from app.db.models import Pool
from app.core.schemas import ListResponse
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import PoolSchema
from .schemas import PoolCreateRequest
from .schemas import PoolUpdateRequest
from .schemas import PoolMembershipSchema
from .repository import PoolRepository


EMPTY = PoolMembershipSchema(
    nodes_total=0,
    nodes_ready=0,
    nodes_cordoned=0,
    devices_total=0,
    devices_healthy=0,
    devices_allocatable=0,
)


def _revision(pool: Pool) -> str:
    """The value `If-Match` is checked against.

    Derived from the last write rather than stored, for the same reason it is on
    a cluster: `updated_at` already moves on every update, and a second column
    tracking the same fact is a second column that can disagree with it.
    """
    stamp = pool.updated_at or pool.created_at
    return str(int(stamp.timestamp() * 1_000_000))


def _membership(row: dict | None) -> PoolMembershipSchema:
    if not row:
        return EMPTY
    return PoolMembershipSchema(
        nodes_total=row.get("nodes_total", 0),
        nodes_ready=row.get("nodes_ready", 0),
        nodes_cordoned=row.get("nodes_cordoned", 0),
        devices_total=row.get("devices_total", 0),
        devices_healthy=row.get("devices_healthy", 0),
        devices_allocatable=row.get("devices_allocatable", 0),
    )


def within_bounds(nodes: int, min_nodes: int, max_nodes: int) -> bool:
    """Whether observed membership sits inside the declared node bounds.

    `max_nodes` of zero means unbounded rather than "no nodes allowed": the
    column defaults to zero, and reading the default as a hard ceiling would put
    every freshly created pool permanently out of bounds.
    """
    if nodes < min_nodes:
        return False
    return max_nodes <= 0 or nodes <= max_nodes


class PoolService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = PoolRepository(db)

    def schema(self, pool: Pool, cluster_uid: str, membership: PoolMembershipSchema) -> PoolSchema:
        return PoolSchema(
            id=pool.uid,
            cluster_id=cluster_uid,
            name=pool.name,
            device_class=pool.device_class,
            devices_per_node=pool.devices_per_node,
            node_shape=pool.node_shape,
            min_nodes=pool.min_nodes,
            max_nodes=pool.max_nodes,
            labels=pool.labels,
            taints=pool.taints,
            enabled=pool.enabled,
            created_at=pool.created_at,
            updated_at=pool.updated_at,
            revision=_revision(pool),
            membership=membership,
            within_declared_bounds=within_bounds(membership.nodes_total, pool.min_nodes, pool.max_nodes),
        )

    async def _one(self, ref: str, cluster: str | None = None, lock: bool = False) -> tuple[Pool, str]:
        row = await self.repo.get(ref, cluster, lock=lock)
        if row is None:
            raise NotFound(f"no pool {ref!r}")
        return row[0], row.cluster_uid

    async def get(self, ref: str, cluster: str | None = None) -> PoolSchema:
        pool, cluster_uid = await self._one(ref, cluster)
        rollup = await self.repo.membership([pool.id])
        return self.schema(pool, cluster_uid, _membership(rollup.get(pool.id)))

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        device_class: str | None = None,
        enabled: bool | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> ListResponse[PoolSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            cluster=cluster,
            device_class=device_class,
            enabled=enabled,
            search=search,
            labels=labels,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        rollup = await self.repo.membership([r[0].id for r in kept])
        items = [self.schema(r[0], r.cluster_uid, _membership(rollup.get(r[0].id))) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)

    async def create(self, data: PoolCreateRequest) -> PoolSchema:
        if data.min_nodes < 0 or data.max_nodes < 0 or data.devices_per_node < 0:
            raise Invalid("node and device counts cannot be negative")
        if data.max_nodes and data.max_nodes < data.min_nodes:
            raise Invalid(
                "max_nodes is below min_nodes",
                {"min_nodes": data.min_nodes, "max_nodes": data.max_nodes},
            )
        cluster = await self.repo.cluster_by_ref(data.cluster)
        if cluster is None:
            raise NotFound(f"no cluster {data.cluster!r}")
        pool = Pool(
            uid=new_uid(POOL),
            cluster_id=cluster.id,
            name=data.name,
            device_class=data.device_class,
            devices_per_node=data.devices_per_node,
            node_shape=data.node_shape,
            min_nodes=data.min_nodes,
            max_nodes=data.max_nodes,
            labels=data.labels,
            taints=data.taints,
            enabled=data.enabled,
        )
        try:
            await self.repo.add(pool)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict(f"pool {data.name!r} already exists in {data.cluster!r}") from exc
        return await self.get(pool.uid)

    async def update(
        self, ref: str, data: PoolUpdateRequest, if_match: str | None = None, cluster: str | None = None
    ) -> PoolSchema:
        pool, _ = await self._one(ref, cluster, lock=True)
        if if_match is not None and if_match != _revision(pool):
            raise Conflict("pool changed since it was read", {"revision": _revision(pool)})

        fields = {k: v for k, v in data.to_dict().items() if v is not UNSET}
        merged_min = fields.get("min_nodes", pool.min_nodes)
        merged_max = fields.get("max_nodes", pool.max_nodes)
        if merged_min < 0 or merged_max < 0:
            raise Invalid("node counts cannot be negative")
        if merged_max and merged_max < merged_min:
            raise Invalid("max_nodes is below min_nodes", {"min_nodes": merged_min, "max_nodes": merged_max})
        for key, value in fields.items():
            setattr(pool, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise Conflict("pool conflicts with an existing one") from exc
        return await self.get(pool.uid)

    async def delete(self, ref: str, cluster: str | None = None) -> None:
        """Refuses while the pool still has nodes in it.

        `Node.pool_id` is `ON DELETE SET NULL`, so deleting a populated pool
        would succeed quietly and leave its nodes pointing at nothing. Nobody
        notices that until placement starts behaving oddly and the nodes it was
        reasoning about have no pool to explain them, so an occupied pool is a
        `Conflict` and draining it is the caller's job first.
        """
        pool, _ = await self._one(ref, cluster, lock=True)
        rollup = await self.repo.membership([pool.id])
        nodes = _membership(rollup.get(pool.id)).nodes_total
        if nodes:
            raise Conflict(
                f"pool {ref!r} still has {nodes} node(s) in it",
                {"nodes_total": nodes},
            )
        await self.repo.delete(pool)
        await self.db.commit()
