from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Cluster
from app.db.models import TopologyDomain
from app.core.filters import ref_ids


class TopologyDomainRepository:
    """Reads only. Domains are discovered by the operator, never declared."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.node = aliased(Node)
        self.parent = aliased(TopologyDomain)

    def _base(self) -> Select:
        return (
            select(TopologyDomain, Cluster.uid, Cluster.name, self.node.uid, self.node.name, self.parent.uid)
            .join(Cluster, TopologyDomain.cluster_id == Cluster.id)
            .outerjoin(self.node, TopologyDomain.node_id == self.node.id)
            .outerjoin(self.parent, TopologyDomain.parent_id == self.parent.id)
        )

    async def get(self, ref: str) -> Row | None:
        """A domain has no unique name, so a non-id reference is its identifier."""
        column = TopologyDomain.uid if is_uid(ref) else TopologyDomain.identifier
        result = await self.db.execute(self._base().where(column == ref))
        return result.first()

    def query(
        self,
        limit: int,
        after_seq: int | None = None,
        cluster: str | None = None,
        node: str | None = None,
        kind: str | None = None,
        spans_nodes: bool | None = None,
    ) -> Select:
        query = self._base().order_by(TopologyDomain.seq.asc()).limit(limit + 1)
        if after_seq is not None:
            query = query.where(TopologyDomain.seq > after_seq)
        if cluster is not None:
            query = query.where(TopologyDomain.cluster_id.in_(ref_ids(Cluster, cluster)))
        if node is not None:
            query = query.where(TopologyDomain.node_id.in_(ref_ids(Node, node)))
        if kind is not None:
            query = query.where(TopologyDomain.kind == kind)
        if spans_nodes is not None:
            query = query.where(TopologyDomain.spans_nodes == spans_nodes)
        return query

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.query(**kwargs))
        return list(result.all())
