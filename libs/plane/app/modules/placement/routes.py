"""Placement: which cluster a workload would go to, asked without creating one.

Three routes, and none of them writes anything, which is why the two POSTs answer
200 instead of 201: they are questions sent with a body, and nothing is created. Placement runs for real inside
deployment creation; this exists because "why did it not land in iad-1" is a
question operators ask before they create anything, and the decision already
carries the answer per cluster. `/expand` is the same question asked about a whole
workload: which clusters it would be split over, and with what share each.

`/reservations` is the third, and it answers "what is about to arrive here". A
cluster with idle accelerators and no free capacity is otherwise inexplicable from
the outside: the devices are visibly unheld, and the thing that spent them is a
deployment nobody thought to look at.
"""

from litestar import Response
from litestar import Controller
from litestar import get
from litestar import post
from litestar.di import Provide

from app.core.context import RequestContext

from .depends import placement_svc_generator
from .schemas import ReservationSchema
from .schemas import PlacementExpandRequest
from .schemas import PlacementPreviewRequest
from .decision import ExpansionDecision
from .decision import PlacementDecision
from .services import PlacementService


class PlacementRoutes(Controller):
    tags = ["Placement"]
    path = "/placement"
    dependencies = {"svc": Provide(placement_svc_generator)}

    @post("/preview", status_code=200)
    async def preview_placement(
        self, context: RequestContext, svc: PlacementService, data: PlacementPreviewRequest
    ) -> Response[PlacementDecision]:
        return Response(await svc.preview(data))

    @post("/expand", status_code=200)
    async def preview_expansion(
        self, context: RequestContext, svc: PlacementService, data: PlacementExpandRequest
    ) -> Response[ExpansionDecision]:
        return Response(await svc.preview_expansion(data))

    @get("/reservations")
    async def list_reservations(
        self,
        context: RequestContext,
        svc: PlacementService,
        cluster: str | None = None,
        device_class: str | None = None,
    ) -> Response[list[ReservationSchema]]:
        """Every promise the fleet is currently carrying, newest expansion included.

        Unpaginated, and that is a considered choice instead of an omission. A
        reservation exists only for as long as a cluster takes to report what it
        bound, so this list is bounded by what is converging right now and not by
        how much the fleet has ever run. A page of it would be a page of an
        aggregate: the same mistake the board avoids by draining every collection
        it counts.
        """
        return Response(await svc.reservations(cluster=cluster, device_class=device_class))
