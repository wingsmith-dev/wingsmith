from litestar import Router
from litestar import Litestar

from app.core.routes import HealthRoutes


def init_app(app: Litestar) -> Litestar:
    """Registers every controller. Registry modules are added here as they land."""
    app.register(Router(path="", route_handlers=[HealthRoutes]))
    return app
