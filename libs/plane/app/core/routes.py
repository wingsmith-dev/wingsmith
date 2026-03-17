from litestar import Response
from litestar import Controller
from litestar import get


class HealthRoutes(Controller):
    tags = ["Main"]

    @get("/health", sync_to_thread=False)
    def health(self) -> Response[dict]:
        return Response({"status": True})
