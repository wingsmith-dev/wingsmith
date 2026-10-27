"""What the gateway itself reads, distinct from what the console and CLI manage
through `/v1/tenants`. Read-only, and the whole reason it is a separate surface
instead of a shape on that one: `key_hashes` must never reach a console
response, and a feed built for a gateway holding the whole table in memory has
different pagination needs than a list a person scrolls.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar.di import Provide
from litestar.params import Parameter

from app.core.context import RequestContext

from .depends import gateway_svc_generator
from .schemas import GatewayTenantTableSchema
from .services import GatewayService


class GatewayRoutes(Controller):
    tags = ["Gateway"]
    path = "/gateway"
    dependencies = {"svc": Provide(gateway_svc_generator)}

    @get("/tenants")
    async def resolve_tenants(
        self,
        context: RequestContext,
        svc: GatewayService,
        if_none_match: str | None = Parameter(header="If-None-Match", default=None),
    ) -> Response[GatewayTenantTableSchema | None]:
        """Host→tenant and hash→key, in one read.

        `If-None-Match` makes the steady state a `304`, the same shape as
        `/endpoints/resolved`: this is polled on the same timer.
        """
        table = await svc.resolve_tenants()
        if if_none_match is not None and if_none_match == table.revision:
            return Response(None, status_code=304, headers={"ETag": table.revision})
        return Response(table, headers={"ETag": table.revision})
