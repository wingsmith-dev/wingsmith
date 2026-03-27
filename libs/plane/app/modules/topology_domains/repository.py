from sqlalchemy import Row
from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import aggregate_order_by

from app.core.ids import is_uid
from app.db.models import Node
from app.db.models import Cluster
from app.db.models import TopologyDomain
from app.db.models import TopologyDomainNode
from app.core.filters import ref_ids


class TopologyDomainRepository:
    """Reads only. Domains are discovered by the operator, never declared.

    Membership comes from `supply_topology_domain_node`, so filtering by node is
    a real containment test: a fabric across four machines matches all four,
    where the old scalar column matched none of them.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.parent = aliased(TopologyDomain)

    def _members(self, column):
        """The domain's membership, aggregated rather than joined into the row.

        A domain that spans four nodes would otherwise return four copies of
        itself, and the page limit counts rows.
        """
        return (
            select(func.coalesce(func.array_agg(aggregate_order_by(column, Node.name.asc())), text("'{}'")))
            .select_from(TopologyDomainNode)
            .join(Node, TopologyDomainNode.node_id == Node.id)
            .where(TopologyDomainNode.domain_id == TopologyDomain.id)
            .scalar_subquery()
        )

    def _base(self) -> Select:
        return (
            select(
                TopologyDomain,
                Cluster.uid,
                Cluster.name,
                self._members(Node.uid).label("node_uids"),
                self._members(Node.name).label("node_names"),
                self.parent.uid,
            )
            .join(Cluster, TopologyDomain.cluster_id == Cluster.id)
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
            query = query.where(
                TopologyDomain.id.in_(
                    select(TopologyDomainNode.domain_id).where(TopologyDomainNode.node_id.in_(ref_ids(Node, node)))
                )
            )
        if kind is not None:
            query = query.where(TopologyDomain.kind == kind)
        if spans_nodes is not None:
            query = query.where(TopologyDomain.spans_nodes == spans_nodes)
        return query

    async def list(self, **kwargs) -> list[Row]:
        result = await self.db.execute(self.query(**kwargs))
        return list(result.all())
