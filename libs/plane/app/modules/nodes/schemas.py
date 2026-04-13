import datetime as dt

from app.core.schemas import Struct


class NodeDeviceRollupSchema(Struct):
    """How many devices this node has, and how they are split by health.

    Carried on the list payload so that "which nodes have unhealthy devices" is
    one aggregate query rather than a device fetch per row. It is a rollup of
    state only — no utilization, no temperature, no power.
    """

    total: int
    by_health: dict[str, int]


class NodeSchema(Struct):
    """A machine observed inside a cluster.

    Declared and observed are not merged, and there is nothing declared here at
    all: a node is discovered, so every field is observed. `cordoned` is its own
    boolean rather than a value of `condition`, because cordon is the state an
    Operation leaves behind and "cordoned" and "not ready" are different facts
    that need different responses.

    Deliberately free of orchestrator-specific fields: os image, kernel, driver
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
    agent_version: str | None
    labels: dict
    taints: list
    devices: NodeDeviceRollupSchema
    joined_at: dt.datetime | None
    observed_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
