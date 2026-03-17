"""Topology domains: observed, read-only.

`GET` list and `GET` one. The route path is hyphenated (`/v1/topology-domains`)
while the Python package is underscored; the wire name follows the rest of the
API's paths and the package name follows the language.

Topology is available both here and embedded on a device. Both are correct: the
embedded form is a display summary and this collection is the authority, which
is why parent and device count appear here and not there.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide

from app.core.context import RequestContext
from app.core.schemas import ListResponse

from .depends import topology_domain_svc_generator
from .schemas import TopologyDomainSchema
from .services import TopologyDomainService


class TopologyDomainRoutes(Controller):
    tags = ["Topology"]
    path = "/topology-domains"
    dependencies = {"svc": Provide(topology_domain_svc_generator)}

    @get()
    async def list_topology_domains(
        self,
        context: RequestContext,
        svc: TopologyDomainService,
        limit: int | None = None,
        cursor: str | None = None,
        cluster: str | None = None,
        node: str | None = None,
        kind: str | None = None,
        spans_nodes: bool | None = None,
    ) -> Response[ListResponse[TopologyDomainSchema]]:
        result = await svc.list(
            limit=limit,
            cursor=cursor,
            cluster=cluster,
            node=node,
            kind=kind,
            spans_nodes=spans_nodes,
        )
        return Response(result)

    @get("/{ref:str}")
    async def get_topology_domain(
        self, context: RequestContext, svc: TopologyDomainService, ref: str
    ) -> Response[TopologyDomainSchema]:
        return Response(await svc.get(ref))
