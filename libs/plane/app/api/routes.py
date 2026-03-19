from litestar import Router
from litestar import Litestar

from app.core.routes import HealthRoutes
from app.modules.nodes.routes import NodeRoutes
from app.modules.pools.routes import PoolRoutes
from app.modules.events.routes import EventRoutes
from app.modules.metrics.routes import MetricsRoutes
from app.modules.quotas.routes import QuotaRoutes
from app.modules.devices.routes import DeviceRoutes
from app.modules.recipes.routes import RecipeRoutes
from app.modules.clusters.routes import ClusterRoutes
from app.modules.policies.routes import PolicyRoutes
from app.modules.projects.routes import ProjectRoutes
from app.modules.artifacts.routes import ArtifactRoutes
from app.modules.endpoints.routes import EndpointRoutes
from app.modules.components.routes import ComponentRoutes
from app.modules.operations.routes import OperationRoutes
from app.modules.allocations.routes import AllocationRoutes
from app.modules.deployments.routes import DeploymentRoutes
from app.modules.priority_classes.routes import PriorityClassRoutes
from app.modules.topology_domains.routes import TopologyDomainRoutes


V1_ROUTES = [
    ClusterRoutes,
    NodeRoutes,
    DeviceRoutes,
    TopologyDomainRoutes,
    PoolRoutes,
    ArtifactRoutes,
    RecipeRoutes,
    DeploymentRoutes,
    EndpointRoutes,
    ProjectRoutes,
    QuotaRoutes,
    PriorityClassRoutes,
    PolicyRoutes,
    AllocationRoutes,
    OperationRoutes,
    ComponentRoutes,
    EventRoutes,
    MetricsRoutes,
]


def init_app(app: Litestar) -> Litestar:
    """Registers every controller. Modules are added to `V1_ROUTES` as they land."""
    app.register(Router(path="", route_handlers=[HealthRoutes]))
    app.register(Router(path="/v1", route_handlers=list(V1_ROUTES)))
    return app
