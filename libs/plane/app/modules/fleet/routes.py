"""The fleet in counts: `GET /v1/fleet/summary`.

Derived, read-only and belonging to no object, like `/v1/clusters/rollups` one
level up: that answers per cluster, this answers for the whole fleet with the
per-class and per-state breakdowns an overview page draws.
"""

from litestar import Response
from litestar import Controller
from litestar import get

from app.core.context import RequestContext

from .schemas import FleetSummarySchema
from .services import FleetService


class FleetRoutes(Controller):
    tags = ["Fleet"]
    path = "/fleet"

    @get("/summary")
    async def fleet_summary(self, context: RequestContext) -> Response[FleetSummarySchema]:
        """Counts of clusters, nodes, devices, deployments, endpoints and workloads across the fleet.

        Each count is one aggregate statement, so the read costs the same for ten
        devices as for ten thousand, and no total is a page size.
        """
        return Response(await FleetService(context.db).summary())
