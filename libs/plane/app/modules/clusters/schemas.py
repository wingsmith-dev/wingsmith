import datetime as dt

from typing import Annotated

from msgspec import Meta

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


# `$` in Python also matches before a trailing newline, and msgspec checks a pattern
# with `re.search`; the lookahead is what keeps "alpha\n" from passing as "alpha".
DNS_LABEL = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$(?!\n)"
LABEL_VALUE = r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$(?!\n)"

ClusterName = Annotated[
    str,
    Meta(
        min_length=1,
        max_length=63,
        pattern=DNS_LABEL,
        description="A DNS label: it becomes the chart's cluster.id, the kube context and the metrics label.",
    ),
]
LabelValue = Annotated[str, Meta(min_length=1, max_length=63, pattern=LABEL_VALUE)]
"""Region, zone and residency: Kubernetes label-value shape, which is what they are matched against."""
MetricsLabel = Annotated[str, Meta(max_length=63, pattern=r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$(?!\n)")]
"""A label value, or empty to clear it."""
DisplayName = Annotated[str, Meta(max_length=200)]


class ClusterSyncSchema(Struct):
    """The observed liveness of a cluster's agent link.

    `state` is derived, not stored: a cluster that has never connected is not the
    same as one that connected and dropped, and neither is a value anybody
    declared.

    One of `never_connected`, `disconnected`, `stale` or `connected`. `stale` is the one
    that is not about the socket — it means the link is up and the cluster has
    stopped saying anything through it, which reads as healthy everywhere that
    only checks whether it is reachable.
    """

    state: str
    last_sync_at: dt.datetime | None
    last_seen_at: dt.datetime | None
    operator_version: str | None
    lag_ms: int | None
    last_error: str | None
    distribution: str | None = None
    cloud: str | None = None
    observed_region: str | None = None
    read_only: bool = False
    multinode: bool = False


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

    `metrics_label` is the `cluster` label this cluster's series carry in the
    shared metrics store. Null means it is not observable and its panels say so,
    rather than querying under a label nobody has confirmed.
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
    metrics_label: str | None


class ClusterSetupSchema(Struct):
    """Everything the register-cluster screen shows to make a cluster ready.

    `values_yaml` is one editable file: the plane-filled identity (cluster id, hub
    URL, operator image) on top and the cluster's own infra facts as blanks below.
    `values_filename` is the name the console saves it under and the helm command
    reads, `hangar-<cluster>-values.yaml`, so it is never confused with the
    chart's own `values.yaml`. `registry_login` is the `helm registry login` a private chart
    registry needs before the pull — `None` when the registry serves anonymously.
    `registry_login_gh` is the same login done with an existing `gh` session
    instead of a hand-made token, and is `None` for any registry that is not
    GitHub's, where `gh` has nothing to say.

    The enrolment token is shown exactly once, when it is minted (issue/rotate):
    `enrolment_token` carries the plaintext then and is folded into `values_yaml`
    (`enrolmentToken.value`) so the chart creates the Secret. On a plain `GET` it
    is `None` and `values_yaml` omits it; `enrolment_token_set` says whether a
    token already exists, so the screen can offer "rotate" instead of "generate".
    """

    helm_command: str
    values_yaml: str
    values_filename: str
    chart_ref: str
    chart_version: str | None
    registry_login: str | None
    registry_login_gh: str | None
    enrolment_token: str | None
    enrolment_token_set: bool


class ClusterHeadroomSchema(Struct):
    """How much of a cluster is free to place onto, derived from observed devices.

    All three numbers are computed at read time from `supply_device`,
    `bind_allocation_device` and `supply_topology_domain`; nothing here is stored.
    A device is free when it is allocatable *and* no live allocation holds it —
    `allocatable` says the accelerator is claimable at all, which is a different
    claim from unclaimed. `largest_contiguous_block` is the biggest set of free
    devices sharing a single topology domain — what a job that wants one fabric
    can actually get — and is not the same as `free_devices`, which is every free
    device regardless of where it sits. `whole_nodes_free` counts nodes on which
    every device is free, the unit a node-granular workload cares about.
    """

    free_devices: int
    whole_nodes_free: int
    largest_contiguous_block: int


class ClusterRollupSchema(Struct):
    """A cluster's observed footprint in aggregate, for the list and card views.

    Every field is derived at read time and nothing is stored. `devices_allocatable`
    is the free count — allocatable and held by no live allocation — and
    `devices_allocated` is `devices_total - devices_allocatable` rather than a
    counted column, because a device is either free or spoken for and a second
    count is a second thing that can disagree with it. `pool_count` and
    `deployment_count` are the declared objects hanging off the cluster; the rest
    are observed.
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
    """A new registry entry. `metrics_label` defaults to the cluster's own name.

    That default is not a guess: `libs/chart/cluster-setup` writes the metrics
    agent's `external_labels.cluster` from the same `cluster.id` the plane renders
    into the install values, which is this name. Pass an explicit null for a
    cluster whose store label is unknown, and the cluster reports no metrics
    source until one is set.
    """

    name: ClusterName
    region: LabelValue
    display_name: DisplayName | None = None
    zone: LabelValue | None = None
    residency: LabelValue | None = None
    phase: str = "pending"
    labels: dict = {}
    capabilities: dict = {}
    metrics_label: MetricsLabel | None | UnsetType = UNSET


class ClusterUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    Clearing `metrics_label` is how a cluster stops being queried at all, and
    setting it is how a cluster whose store label differs from its name is
    corrected without a redeploy.
    """

    display_name: DisplayName | None | UnsetType = UNSET
    region: LabelValue | UnsetType = UNSET
    zone: LabelValue | None | UnsetType = UNSET
    residency: LabelValue | None | UnsetType = UNSET
    phase: str | UnsetType = UNSET
    labels: dict | UnsetType = UNSET
    capabilities: dict | UnsetType = UNSET
    metrics_label: MetricsLabel | None | UnsetType = UNSET


class TunnelAuthRequest(Struct):
    """The enrolment token a serving agent presented when it dialled the gateway.

    The token travels in the body and not in `Authorization`, because that header
    already carries the caller's own credential. One request, two credentials, and
    only one of them belongs to the caller.
    """

    token: Annotated[str, Meta(max_length=512)]


class TunnelIdentitySchema(Struct):
    """Which cluster a verified agent token belongs to, and nothing else.

    No token material, no hash, no prefix. The caller sent the credential and
    gets back only the identity it maps to — enough to key a tunnel by cluster
    and to label a metric, which is all the gateway does with it.
    """

    cluster_id: str
    cluster_name: str


class ClusterServingRequest(Struct):
    """A full replacement. Serving config is small enough that partial updates buy nothing."""

    url: Annotated[str, Meta(max_length=2048)]
    credential_id: Annotated[str, Meta(max_length=64)] | None = None
    enabled: bool = True
