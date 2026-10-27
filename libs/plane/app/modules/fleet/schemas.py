import datetime as dt

from app.core.schemas import Struct


class FleetClustersSchema(Struct):
    """Registered clusters. `reachable` is the hub's view: a live session now."""

    total: int
    reachable: int
    by_phase: dict[str, int]


class FleetNodesSchema(Struct):
    """Observed nodes. `by_condition` is keyed on the node's reported condition."""

    total: int
    ready: int
    cordoned: int
    by_condition: dict[str, int]


class FleetDeviceClassSchema(Struct):
    """One accelerator class across the fleet, the row a capacity panel draws."""

    device_class: str
    total: int
    free: int
    allocated: int


class FleetDevicesSchema(Struct):
    """Observed accelerators.

    `free` is claimable and held by no live allocation, and `allocated` is
    `total - free`, the same split `GET /v1/clusters/rollups` makes per cluster.
    A device's own `allocatable` flag says the card can be claimed at all, so
    counting `allocatable: false` is not the allocated count.
    """

    total: int
    free: int
    allocated: int
    by_health: dict[str, int]
    by_class: list[FleetDeviceClassSchema]


class FleetDeploymentsSchema(Struct):
    """Deployments and the replicas they run.

    `replicas_ready` counts ready replicas fleet-wide and `clusters_serving` the
    clusters carrying at least one. `unschedulable_replicas` is the shortfall
    (`desired - ready`) of every deployment outside `running` for longer than
    `stuck_after_seconds`, timed from `placed_at`, else `admitted_at`, else
    `queued_at`; a deployment with none of those stamps counts too.
    """

    total: int
    by_phase: dict[str, int]
    replicas_ready: int
    clusters_serving: int
    unschedulable_replicas: int
    stuck_after_seconds: int


class FleetEndpointsSchema(Struct):
    """Endpoints by what their own targets say, never by measured traffic.

    Disabled endpoints are counted in `disabled` and nowhere else. An enabled
    endpoint is `idle` with no enabled target, `down` when its targets have no
    ready replica, `degraded` when they have fewer ready than desired, and
    `serving` otherwise.
    """

    total: int
    disabled: int
    serving: int
    degraded: int
    down: int
    idle: int


class FleetWorkloadsSchema(Struct):
    total: int
    paused: int


class FleetSummarySchema(Struct):
    """The fleet in counts, for an overview page that would otherwise drain every
    collection to add them up. Derived at read time; nothing here is stored."""

    clusters: FleetClustersSchema
    nodes: FleetNodesSchema
    devices: FleetDevicesSchema
    deployments: FleetDeploymentsSchema
    endpoints: FleetEndpointsSchema
    workloads: FleetWorkloadsSchema
    generated_at: dt.datetime
