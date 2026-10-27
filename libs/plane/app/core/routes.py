from litestar import Response
from litestar import Controller
from litestar import get

from app.core.params import ACCEPTS_ANY_QUERY


class HealthRoutes(Controller):
    tags = ["Main"]

    # A load balancer's check may append its own params; none of them is a filter.
    @get("/health", sync_to_thread=False, opt={ACCEPTS_ANY_QUERY: True})
    def health(self) -> Response[dict]:
        return Response({"status": True})
