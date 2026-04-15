"""Placement: which cluster a workload would go to, asked without creating one.

Two routes, and neither writes anything. Placement runs for real inside deployment
creation; this exists because "why did it not land in iad-1" is a question
operators ask before they create anything, and the decision already carries the
answer per cluster. `/expand` is the same question asked about a whole workload:
which clusters it would be split over, and with what share each.
"""

from litestar import Response
from litestar import Controller
from litestar import post
from litestar.di import Provide

from app.core.context import RequestContext

from .depends import placement_svc_generator
from .schemas import PlacementExpandRequest
from .schemas import PlacementPreviewRequest
from .decision import ExpansionDecision
from .decision import PlacementDecision
from .services import PlacementService


class PlacementRoutes(Controller):
    tags = ["Placement"]
    path = "/placement"
    dependencies = {"svc": Provide(placement_svc_generator)}

    @post("/preview")
    async def preview_placement(
        self, context: RequestContext, svc: PlacementService, data: PlacementPreviewRequest
    ) -> Response[PlacementDecision]:
        return Response(await svc.preview(data))

    @post("/expand")
    async def preview_expansion(
        self, context: RequestContext, svc: PlacementService, data: PlacementExpandRequest
    ) -> Response[ExpansionDecision]:
        return Response(await svc.preview_expansion(data))
