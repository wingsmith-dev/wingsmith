from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TopologyDomain
from app.core.schemas import ListResponse
from app.core.exceptions import NotFound
from app.core.pagination import page
from app.core.pagination import clamp_limit
from app.core.pagination import decode_cursor

from .schemas import TopologyDomainSchema
from .repository import TopologyDomainRepository


class TopologyDomainService:
    """Read-only, by construction rather than by refusal.

    Topology is observed — the operator discovers the fabric — so no create,
    update or delete method exists on this class.

    This collection and the summary embedded on a device are both correct and
    serve different questions: the embedded form exists so the CLI can print a
    device's domain inline without a second call, and it carries no parent and
    no device count. Nesting and membership are answered here, and this is the
    authority.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = TopologyDomainRepository(db)

    def schema(self, row: Row) -> TopologyDomainSchema:
        domain: TopologyDomain = row[0]
        return TopologyDomainSchema(
            id=domain.uid,
            cluster=row[1],
            cluster_name=row[2],
            nodes=list(row[3]),
            node_names=list(row[4]),
            parent=row[5],
            kind=domain.kind,
            identifier=domain.identifier,
            link_generation=domain.link_generation,
            link_bandwidth_gbps=domain.link_bandwidth_gbps,
            device_count=domain.device_count,
            spans_nodes=domain.spans_nodes,
            attributes=domain.attributes,
            observed_at=domain.observed_at,
            created_at=domain.created_at,
            updated_at=domain.updated_at,
        )

    async def get(self, ref: str) -> TopologyDomainSchema:
        row = await self.repo.get(ref)
        if row is None:
            raise NotFound(f"no topology domain {ref!r}")
        return self.schema(row)

    async def list(
        self,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        node: str | None = None,
        kind: str | None = None,
        spans_nodes: bool | None = None,
    ) -> ListResponse[TopologyDomainSchema]:
        size = clamp_limit(limit)
        rows = await self.repo.list(
            limit=size,
            after_seq=decode_cursor(cursor),
            cluster=cluster,
            node=node,
            kind=kind,
            spans_nodes=spans_nodes,
        )
        kept, next_cursor = page(rows, size, lambda r: r[0].seq)
        items = [self.schema(r) for r in kept]
        return ListResponse(items=items, count=len(items), next_cursor=next_cursor)
