import datetime as dt

from app.core.schemas import Struct


class NodeConditionSchema(Struct):
    """One observed node condition as the cluster reported it.

    Generic health signals (memory or disk pressure, network availability) kept in full rather
    than reduced to the single `condition` summary. The plane does not interpret `type`; it is
    whatever the cluster named the signal.
    """

    type: str
    status: str
    reason: str
    message: str
    since: str


class NodeDeviceRollupSchema(Struct):
    """How many devices this node has, and how they are split by health.

    Carried on the list payload so that "which nodes have unhealthy devices" is
    one aggregate query instead of a device fetch per row. It is a rollup of
    state only: no utilization, no temperature, no power.

    `device_class` is the dominant GPU model on the node, derived at read time
    from the node's devices, not stored, and `None` when the node carries
    no devices. `allocated` is how many of `total` are committed to a live
    allocation right now.
    """

    total: int
    by_health: dict[str, int]
    device_class: str | None = None
    allocated: int = 0


class NodesSummarySchema(Struct):
    """Fleet-wide node rollup, computed server-side across every node.

    A single figure for the list page header instead of a count derived from a
    page. `faulted_devices` counts accelerators whose observed health is not
    `healthy`, across all nodes; it is a health rollup, never telemetry.
    """

    total: int
    ready: int
    cordoned: int
    faulted_devices: int


class NodeSchema(Struct):
    """A machine observed inside a cluster.

    Declared and observed are not merged, and there is nothing declared here at
    all: a node is discovered, so every field is observed. `cordoned` is its own
    boolean instead of a value of `condition`, because cordon is the state an
    Operation leaves behind and "cordoned" and "not ready" are different facts
    that need different responses.

    Free of orchestrator-specific fields: os image, kernel, driver
    and agent version are generic observed attributes, and no kubelet version or
    manifest fragment appears here.
    """

    id: str
    name: str
    cluster: str
    cluster_name: str
    pool: str | None
    pool_name: str | None
    condition: str
    conditions: list[NodeConditionSchema]
    cordoned: bool
    ready: bool
    instance_type: str | None
    cloud: str | None
    zone: str | None
    region: str | None
    cpu_cores: int | None
    memory_mib: int | None
    cpu_arch: str | None
    os_image: str | None
    kernel_version: str | None
    driver_version: str | None
    operator_version: str | None
    labels: dict
    taints: list
    devices: NodeDeviceRollupSchema
    joined_at: dt.datetime | None
    observed_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
