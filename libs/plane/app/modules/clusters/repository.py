import datetime as dt

from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import or_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import delete
from sqlalchemy import exists
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import aliased
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Pool
from app.db.models import Event
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Workload
from app.db.models import Operation
from app.db.models import Allocation
from app.db.models import Credential
from app.db.models import Deployment
from app.db.models import BenchmarkRun
from app.db.models import ClusterServing
from app.db.models import ClusterSyncState
from app.db.supply import device_free
from app.db.supply import free_device_sum


OPEN_PHASES = ("pending", "running")
DELETED_OPERATION = "the cluster was deleted before it reported an outcome"
DELETED_RUN = "the cluster running this benchmark was deleted before it reported a result"


class ClusterRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return select(Cluster).options(selectinload(Cluster.sync_state), selectinload(Cluster.serving))

    async def get(self, ref: str, lock: bool = False) -> Cluster | None:
        """Resolves either an id or a name. Prefixes are the client's problem.

        `lock` holds the cluster row until the transaction ends, so an If-Match check
        and the write it guards see the same revision.
        """
        column = Cluster.uid if is_uid(ref) else Cluster.name
        query = self._base().where(column == ref)
        if lock:
            query = query.with_for_update(of=Cluster).execution_options(populate_existing=True)
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def by_token_hash(self, token_hash: str) -> Cluster | None:
        """The cluster a per-cluster enrolment token belongs to, by its hash.

        No eager loads: the one caller is hub authentication, which needs only
        the cluster's identity (uid, name) to bind the session.
        """
        result = await self.db.execute(select(Cluster).where(Cluster.token_hash == token_hash))
        return result.scalar_one_or_none()

    async def list(
        self,
        limit: int,
        after_seq: int | None = None,
        region: str | None = None,
        phase: str | None = None,
        search: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[Cluster]:
        query = self._base().order_by(Cluster.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(Cluster.seq > after_seq)
        if region is not None:
            query = query.where(Cluster.region == region)
        if phase is not None:
            query = query.where(Cluster.phase == phase)
        if search is not None:
            query = query.where(Cluster.name.ilike(f"%{search}%"))
        for key, value in (labels or {}).items():
            query = query.where(Cluster.labels[key].astext == value)
        result = await self.db.execute(query)
        return list(result.scalars().unique())

    def node_rollup_query(self, cluster_ids: list) -> Select:
        return (
            select(
                Node.cluster_id,
                func.count(Node.id).label("nodes_total"),
                func.sum(case((Node.ready.is_(True), 1), else_=0)).label("nodes_ready"),
            )
            .where(Node.cluster_id.in_(cluster_ids))
            .group_by(Node.cluster_id)
        )

    def device_rollup_query(self, cluster_ids: list) -> Select:
        return (
            select(
                Device.cluster_id,
                func.count(Device.id).label("devices_total"),
                free_device_sum().label("devices_allocatable"),
            )
            .where(Device.cluster_id.in_(cluster_ids))
            .group_by(Device.cluster_id)
        )

    def pool_rollup_query(self, cluster_ids: list) -> Select:
        return (
            select(Pool.cluster_id, func.count(Pool.id).label("pool_count"))
            .where(Pool.cluster_id.in_(cluster_ids))
            .group_by(Pool.cluster_id)
        )

    def deployment_rollup_query(self, cluster_ids: list) -> Select:
        return (
            select(Deployment.cluster_id, func.count(Deployment.id).label("deployment_count"))
            .where(Deployment.cluster_id.in_(cluster_ids))
            .group_by(Deployment.cluster_id)
        )

    def whole_nodes_free_query(self, cluster_ids: list) -> Select:
        """Counts nodes on which every device is free.

        Reduced entirely in the database: a per-node subquery totals devices and
        free devices, and the outer query keeps only nodes where the two agree and
        at least one device exists.
        """
        per_node = (
            select(
                Device.cluster_id.label("cluster_id"),
                Device.node_id.label("node_id"),
                func.count(Device.id).label("total"),
                free_device_sum().label("free"),
            )
            .where(Device.cluster_id.in_(cluster_ids))
            .group_by(Device.cluster_id, Device.node_id)
            .subquery()
        )
        return (
            select(per_node.c.cluster_id, func.count().label("whole_nodes_free"))
            .where(per_node.c.total == per_node.c.free)
            .group_by(per_node.c.cluster_id)
        )

    def largest_block_query(self, cluster_ids: list) -> Select:
        """Largest set of free devices sharing one topology domain, per cluster.

        Devices without a topology domain are not part of any interconnect fabric,
        so they are excluded here; the service floors the result at one when a
        cluster has any free device at all.
        """
        per_domain = (
            select(
                Device.cluster_id.label("cluster_id"),
                Device.topology_domain_id.label("domain_id"),
                func.count(Device.id).label("cnt"),
            )
            .where(
                Device.cluster_id.in_(cluster_ids),
                device_free(),
                Device.topology_domain_id.is_not(None),
            )
            .group_by(Device.cluster_id, Device.topology_domain_id)
            .subquery()
        )
        return select(per_domain.c.cluster_id, func.max(per_domain.c.cnt).label("largest_block")).group_by(
            per_domain.c.cluster_id
        )

    async def rollup(self, cluster_ids: list) -> dict:
        """Assembles every per-cluster rollup number into one keyed dict.

        Clusters with no observed supply are simply absent from the result; the
        service fills them with zeros. Nothing is written.
        """
        rollup: dict = {}
        if not cluster_ids:
            return rollup

        nodes = await self.db.execute(self.node_rollup_query(cluster_ids))
        for row in nodes.all():
            rollup.setdefault(row.cluster_id, {}).update(
                nodes_total=row.nodes_total or 0,
                nodes_ready=row.nodes_ready or 0,
            )
        devices = await self.db.execute(self.device_rollup_query(cluster_ids))
        for row in devices.all():
            rollup.setdefault(row.cluster_id, {}).update(
                devices_total=row.devices_total or 0,
                devices_allocatable=row.devices_allocatable or 0,
            )
        pools = await self.db.execute(self.pool_rollup_query(cluster_ids))
        for row in pools.all():
            rollup.setdefault(row.cluster_id, {}).update(pool_count=row.pool_count or 0)
        deployments = await self.db.execute(self.deployment_rollup_query(cluster_ids))
        for row in deployments.all():
            rollup.setdefault(row.cluster_id, {}).update(deployment_count=row.deployment_count or 0)
        whole = await self.db.execute(self.whole_nodes_free_query(cluster_ids))
        for row in whole.all():
            rollup.setdefault(row.cluster_id, {}).update(whole_nodes_free=row.whole_nodes_free or 0)
        blocks = await self.db.execute(self.largest_block_query(cluster_ids))
        for row in blocks.all():
            rollup.setdefault(row.cluster_id, {}).update(largest_block=row.largest_block or 0)
        return rollup

    async def all_ids(self, limit: int, after_seq: int | None = None) -> list:
        """One page of `(id, uid, seq)` triples, ordered, for the batch rollup.

        Paged on the same key as `list` so the two collections can be walked
        together: a console joining rollups onto cluster rows follows one cursor
        per collection and never has to reconcile two orderings.
        """
        query = select(Cluster.id, Cluster.uid, Cluster.seq).order_by(Cluster.seq.asc()).limit(limit)
        if after_seq is not None:
            query = query.where(Cluster.seq > after_seq)
        result = await self.db.execute(query)
        return list(result.all())

    async def add(self, cluster: Cluster) -> Cluster:
        self.db.add(cluster)
        self.db.add(ClusterSyncState(cluster=cluster))
        await self.db.flush()
        return cluster

    async def credential(self, credential_id: UUID) -> Credential | None:
        result = await self.db.execute(select(Credential).where(Credential.id == credential_id))
        return result.scalar_one_or_none()

    async def set_serving(self, cluster: Cluster, serving: ClusterServing) -> ClusterServing:
        self.db.add(serving)
        await self.db.flush()
        return serving

    async def clear_serving(self, serving: ClusterServing) -> None:
        await self.db.delete(serving)
        await self.db.flush()

    def holds_query(self, cluster_id: UUID) -> Select:
        """What still depends on a cluster: deployments on it, allocations it has
        not released, and workloads pinned to it.

        All three are counted rather than left to the foreign keys, because the
        keys cascade (deployments, allocations) or quietly unpin (workloads),
        and a delete would take them along without anyone having asked.
        """
        deployments = select(func.count(Deployment.id)).where(Deployment.cluster_id == cluster_id).scalar_subquery()
        allocations = (
            select(func.count(Allocation.id))
            .where(Allocation.cluster_id == cluster_id, Allocation.released_at.is_(None))
            .scalar_subquery()
        )
        pinned = select(func.count(Workload.id)).where(Workload.pinned_cluster_id == cluster_id).scalar_subquery()
        return select(
            deployments.label("deployments"),
            allocations.label("allocations"),
            pinned.label("pinned_workloads"),
        )

    async def holds(self, cluster_id: UUID) -> dict[str, int]:
        row = (await self.db.execute(self.holds_query(cluster_id))).one()
        return {key: int(value or 0) for key, value in row._mapping.items()}

    async def delete(self, cluster: Cluster) -> None:
        """Deletes the row; its operations, benchmark runs, events and alerts stay, with no cluster.

        Open operations and runs are cancelled first, in the same transaction. Once the
        cluster is gone nothing can report on them or be handed them again, and the
        reactor only times out operations that carry a deadline, so left open they would
        read as pending for ever.

        An event whose cluster becomes null can match one already held with no cluster on
        every column of `uq_ops_event_occurrence`, which would refuse the delete. That pair
        is one occurrence recorded twice, so this cluster's copy goes first.
        """
        moment = dt.datetime.now(dt.UTC)
        closed = select(Operation.id).where(Operation.cluster_id == cluster.id, Operation.phase.in_(OPEN_PHASES))
        await self.db.execute(
            update(BenchmarkRun)
            .where(
                or_(BenchmarkRun.cluster_id == cluster.id, BenchmarkRun.operation_id.in_(closed)),
                BenchmarkRun.phase.in_(OPEN_PHASES),
            )
            .values(phase="cancelled", finished_at=moment, error=DELETED_RUN)
        )
        await self.db.execute(
            update(Operation)
            .where(Operation.cluster_id == cluster.id, Operation.phase.in_(OPEN_PHASES))
            .values(phase="cancelled", finished_at=moment, error=DELETED_OPERATION)
        )
        kept = aliased(Event)
        await self.db.execute(
            delete(Event).where(
                Event.cluster_id == cluster.id,
                exists().where(
                    kept.cluster_id.is_(None),
                    kept.type == Event.type,
                    kept.subject_type == Event.subject_type,
                    kept.occurred_at == Event.occurred_at,
                    func.md5(kept.message) == func.md5(Event.message),
                ),
            )
        )
        await self.db.delete(cluster)
        await self.db.flush()
