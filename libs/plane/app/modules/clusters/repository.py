from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Pool
from app.db.models import Device
from app.db.models import Cluster
from app.db.models import Credential
from app.db.models import Deployment
from app.db.models import ClusterServing
from app.db.models import ClusterSyncState


class ClusterRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self) -> Select:
        return select(Cluster).options(selectinload(Cluster.sync_state), selectinload(Cluster.serving))

    async def get(self, ref: str) -> Cluster | None:
        """Resolves either an id or a name. Prefixes are the client's problem."""
        column = Cluster.uid if is_uid(ref) else Cluster.name
        result = await self.db.execute(self._base().where(column == ref))
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
                func.sum(case((Device.allocatable.is_(True), 1), else_=0)).label("devices_allocatable"),
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
        """Counts nodes on which every device is allocatable.

        Reduced entirely in the database: a per-node subquery totals devices and
        allocatable devices, and the outer query keeps only nodes where the two
        agree and at least one device exists.
        """
        per_node = (
            select(
                Device.cluster_id.label("cluster_id"),
                Device.node_id.label("node_id"),
                func.count(Device.id).label("total"),
                func.sum(case((Device.allocatable.is_(True), 1), else_=0)).label("free"),
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
        """Largest set of allocatable devices sharing one topology domain, per cluster.

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
                Device.allocatable.is_(True),
                Device.topology_domain_id.is_not(None),
            )
            .group_by(Device.cluster_id, Device.topology_domain_id)
            .subquery()
        )
        return (
            select(per_domain.c.cluster_id, func.max(per_domain.c.cnt).label("largest_block"))
            .group_by(per_domain.c.cluster_id)
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

    async def all_ids(self) -> list:
        """Every cluster's (id, uid) pair, ordered, for the batch rollup."""
        result = await self.db.execute(select(Cluster.id, Cluster.uid).order_by(Cluster.seq.asc()))
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

    async def delete(self, cluster: Cluster) -> None:
        await self.db.delete(cluster)
        await self.db.flush()
