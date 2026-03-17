from litestar import Router
from litestar import Litestar

from app.core.routes import HealthRoutes
from app.modules.nodes.routes import NodeRoutes
from app.modules.pools.routes import PoolRoutes
from app.modules.events.routes import EventRoutes
from app.modules.devices.routes import DeviceRoutes
from app.modules.clusters.routes import ClusterRoutes
from app.modules.topology_domains.routes import TopologyDomainRoutes


V1_ROUTES = [
    ClusterRoutes,
    NodeRoutes,
    DeviceRoutes,
    TopologyDomainRoutes,
    PoolRoutes,
    EventRoutes,
]


def init_app(app: Litestar) -> Litestar:
    """Registers every controller. Modules are added to `V1_ROUTES` as they land."""
    app.register(Router(path="", route_handlers=[HealthRoutes]))
    app.register(Router(path="/v1", route_handlers=list(V1_ROUTES)))
    return app
