import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class ClusterSyncSchema(Struct):
    """The observed liveness of a cluster's agent link.

    `state` is derived, not stored: a cluster that has never connected is not the
    same as one that connected and dropped, and neither is a value anybody
    declared.

    One of `never`, `disconnected`, `stale` or `connected`. `stale` is the one
    that is not about the socket — it means the link is up and the cluster has
    stopped saying anything through it, which reads as healthy everywhere that
    only checks whether it is reachable.
    """

    state: str
    last_sync_at: dt.datetime | None
    last_seen_at: dt.datetime | None
    agent_version: str | None
    lag_ms: int | None
    last_error: str | None
    distribution: str | None = None
    cloud: str | None = None
    observed_region: str | None = None


class ClusterServingSchema(Struct):
    """The cluster's data-plane address, as configuration — never resolved here.

    `credential_id` is a reference, never the secret: the plane hands out the
    pointer and the gateway is what redeems it.
    """

    url: str
    credential_id: str | None
    enabled: bool


class ClusterSchema(Struct):
    """Declared and observed returned together, never merged.

    `phase` is what we asked for; `reachable` and `sync` are what is true right
    now. Folding the two would make "someone disabled this cluster" and "the
    network is down" the same fact, and they need different responses.
    """

    id: str
    name: str
    display_name: str | None
    region: str
    zone: str | None
    residency: str | None
    phase: str
    labels: dict
    capabilities: dict
    enrolled_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str
    reachable: bool
    sync: ClusterSyncSchema
    serving: ClusterServingSchema | None


class ClusterSetupSchema(Struct):
    """Everything the register-cluster screen shows to make a cluster ready.

    `values_yaml` is the per-cluster identity block (cluster id, hub URL, operator
    image), merged by the operator with a local values file carrying the cluster's
    own infra facts. `registry_login` is the `helm registry login` a private chart
    registry needs before the pull — `None` when the registry serves anonymously.

    The enrolment token is shown exactly once, when it is minted (issue/rotate):
    `enrolment_token` carries the plaintext then and is folded into `values_yaml`
    (`enrolmentToken.value`) so the chart creates the Secret. On a plain `GET` it
    is `None` and `values_yaml` omits it; `enrolment_token_set` says whether a
    token already exists, so the screen can offer "rotate" instead of "generate".
    """

    helm_command: str
    values_yaml: str
    chart_ref: str
    chart_version: str | None
    registry_login: str | None
    enrolment_token: str | None
    enrolment_token_set: bool


class ClusterHeadroomSchema(Struct):
    """How much of a cluster is free to place onto, derived from observed devices.

    All three numbers are computed at read time from `supply_device` and
    `supply_topology_domain`; nothing here is stored. `largest_contiguous_block`
    is the biggest set of allocatable devices sharing a single topology domain —
    what a job that wants one fabric can actually get — and is not the same as
    `free_devices`, which is every allocatable device regardless of where it sits.
    `whole_nodes_free` counts nodes on which every device is allocatable, the unit
    a node-granular workload cares about.
    """

    free_devices: int
    whole_nodes_free: int
    largest_contiguous_block: int


class ClusterRollupSchema(Struct):
    """A cluster's observed footprint in aggregate, for the list and card views.

    Every field is derived at read time and nothing is stored. `devices_allocated`
    is `devices_total - devices_allocatable` rather than a counted column, because
    allocation is the absence of allocatability and a second count is a second
    thing that can disagree with it. `pool_count` and `deployment_count` are the
    declared objects hanging off the cluster; the rest are observed.
    """

    cluster_id: str
    nodes_total: int
    nodes_ready: int
    devices_total: int
    devices_allocatable: int
    devices_allocated: int
    pool_count: int
    deployment_count: int
    headroom: ClusterHeadroomSchema


class ClusterCreateRequest(Struct):
    name: str
    region: str
    display_name: str | None = None
    zone: str | None = None
    residency: str | None = None
    phase: str = "pending"
    labels: dict = {}
    capabilities: dict = {}


class ClusterUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears."""

    display_name: str | None | UnsetType = UNSET
    region: str | UnsetType = UNSET
    zone: str | None | UnsetType = UNSET
    residency: str | None | UnsetType = UNSET
    phase: str | UnsetType = UNSET
    labels: dict | UnsetType = UNSET
    capabilities: dict | UnsetType = UNSET


class ClusterServingRequest(Struct):
    """A full replacement. Serving config is small enough that partial updates buy nothing."""

    url: str
    credential_id: str | None = None
    enabled: bool = True
