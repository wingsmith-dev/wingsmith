from litestar import Router
from litestar import Litestar

from app.auth import AuthRoutes
from app.api.spa import ConsoleRoutes
from app.api.spa import has_console
from app.api.metrics import PrometheusRoutes
from app.core.routes import HealthRoutes
from app.core.system_routes import SystemRoutes
from app.modules.fleet.routes import FleetRoutes
from app.modules.nodes.routes import NodeRoutes
from app.modules.pools.routes import PoolRoutes
from app.modules.alerts.routes import AlertRoutes
from app.modules.alerts.routes import AlertRuleRoutes
from app.modules.alerts.routes import AlertThresholdRoutes
from app.modules.events.routes import EventRoutes
from app.modules.quotas.routes import QuotaRoutes
from app.modules.quotas.routes import ServingQuotaRoutes
from app.modules.devices.routes import DeviceRoutes
from app.modules.gateway.routes import GatewayRoutes
from app.modules.metrics.routes import MetricsRoutes
from app.modules.serving.routes import ServingRoutes
from app.modules.serving.routes import TenantServingRoutes
from app.modules.tenants.routes import TenantRoutes
from app.modules.artifacts.usage import ArtifactUsageRoutes
from app.modules.clusters.routes import ClusterRoutes
from app.modules.policies.routes import PolicyRoutes
from app.modules.projects.routes import ProjectRoutes
from app.modules.artifacts.routes import ArtifactRoutes
from app.modules.endpoints.routes import EndpointRoutes
from app.modules.placement.routes import PlacementRoutes
from app.modules.workloads.routes import WorkloadRoutes
from app.modules.benchmarks.routes import BenchmarkRoutes
from app.modules.components.routes import ComponentRoutes
from app.modules.discovered.routes import DiscoveredWorkloadRoutes
from app.modules.operations.routes import OperationRoutes
from app.modules.allocations.routes import AllocationRoutes
from app.modules.deployments.routes import DeploymentRoutes
from app.modules.notifications.routes import NotificationChannelRoutes
from app.modules.device_classes.routes import DeviceClassRoutes
from app.modules.priority_classes.routes import PriorityClassRoutes
from app.modules.topology_domains.routes import TopologyDomainRoutes


V1_ROUTES = [
    ClusterRoutes,
    FleetRoutes,
    NodeRoutes,
    DeviceRoutes,
    DeviceClassRoutes,
    TopologyDomainRoutes,
    PoolRoutes,
    ArtifactRoutes,
    ArtifactUsageRoutes,
    WorkloadRoutes,
    DeploymentRoutes,
    PlacementRoutes,
    DiscoveredWorkloadRoutes,
    EndpointRoutes,
    TenantRoutes,
    GatewayRoutes,
    ProjectRoutes,
    QuotaRoutes,
    ServingQuotaRoutes,
    PriorityClassRoutes,
    PolicyRoutes,
    AllocationRoutes,
    OperationRoutes,
    BenchmarkRoutes,
    ComponentRoutes,
    EventRoutes,
    AlertRoutes,
    AlertThresholdRoutes,
    AlertRuleRoutes,
    NotificationChannelRoutes,
    MetricsRoutes,
    ServingRoutes,
    TenantServingRoutes,
]


def init_app(app: Litestar) -> Litestar:
    """Registers every controller. Modules are added to `V1_ROUTES` as they land."""
    app.register(Router(path="", route_handlers=[HealthRoutes, PrometheusRoutes]))
    app.register(Router(path="/v1", route_handlers=[AuthRoutes, SystemRoutes, *V1_ROUTES]))
    # Registered last and only when the image carries a build: the console is a
    # catch-all, and an image built without one should 404, not answer
    # every unmatched path with a missing file.
    if has_console():
        app.register(Router(path="", route_handlers=[ConsoleRoutes]))
    return app
