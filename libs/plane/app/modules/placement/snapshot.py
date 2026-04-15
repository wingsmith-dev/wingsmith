"""The input side of placement: a plain, ORM-free picture of demand and supply.

Every struct here is something a caller builds by reading the database and
handing in. Nothing in this module knows what a session is, and accepting an ORM
row anywhere would quietly reintroduce lazy loads — and with them I/O — into a
function whose whole value is that it has none.
"""

from msgspec import Struct


class Snap(Struct, frozen=True, kw_only=True):
    """Base for every snapshot struct: immutable and keyword-constructed."""


class DeviceHold(Snap):
    """Who currently owns a device, and whether that ownership can be taken away.

    Present only on committed devices. Priority and preemptibility ride along
    because preemption has to be decided from the snapshot alone — going back to
    look up the holder's priority class would be I/O.
    """

    workload: str
    project: str
    priority_value: int
    preemptible: bool
    deployment: str | None = None


class DeviceSnapshot(Snap):
    """One accelerator, as observed. The unit every capacity number is counted in.

    A device is free when its node will take work, and it is healthy, allocatable,
    and unheld — four separate facts, because a device can be idle and still
    unusable.

    `schedulable` is the node's fact, carried down onto the device because
    placement counts devices and never looks at a node directly. It is the same
    judgement the in-cluster scheduler makes in `reconcile.nodeUsable`, and it
    has to be made in both places: rule 3 leaves device selection to the cluster,
    but a plane that ignores it picks a cluster on capacity that cluster will
    refuse, and the workload bounces instead of landing somewhere it fits.
    """

    uid: str
    node: str
    device_class: str
    memory_mib: int = 0
    healthy: bool = True
    allocatable: bool = True
    schedulable: bool = True
    domain: str | None = None
    held_by: DeviceHold | None = None

    @property
    def free(self) -> bool:
        return self.schedulable and self.healthy and self.allocatable and self.held_by is None


class TopologyDomainSnapshot(Snap):
    """A set of devices sharing an interconnect, with its nesting preserved.

    `parent` matters because a request for an NVLink domain must not be satisfied
    by counting a NUMA node's devices, and `spans_nodes` matters because a domain
    is no longer a synonym for a chassis.
    """

    uid: str
    kind: str
    identifier: str
    parent: str | None = None
    spans_nodes: bool = False


class ClusterSnapshot(Snap):
    """One cluster's placement-relevant surface: what it is, and what it holds free.

    `phase` and `reachable` are kept apart on purpose. Phase is declared — we say
    a cluster is draining. Reachable is observed — the agent either has a live
    session or it does not. A cluster can be perfectly reachable and still not a
    placement target, and it can be declared active while nothing is listening.
    """

    uid: str
    name: str
    region: str
    phase: str = "active"
    reachable: bool = True
    zone: str | None = None
    residency: str | None = None
    devices: tuple[DeviceSnapshot, ...] = ()
    domains: tuple[TopologyDomainSnapshot, ...] = ()
    labels: dict[str, str] = {}
    taken_names: dict[str, str] = {}
    """Deployment name -> the uid of the workload that owns it, in this cluster.

    A deployment name is unique per cluster, so expanding a workload onto a cluster
    where its name already belongs to someone else cannot succeed. Placement rejects
    that cluster as a candidate rather than inventing a suffix, and carries the map
    in the snapshot so the check stays a pure comparison.
    """


class QuotaSnapshot(Snap):
    """A project's floor, ceiling, and current draw for one device class.

    `cluster` is None for a quota that spans every cluster; a cluster-scoped quota
    wins over a global one for that cluster. `allocated_devices` is the observed
    half and is never merged with the declared numbers.
    """

    project: str
    device_class: str
    cluster: str | None = None
    guaranteed_devices: int = 0
    limit_devices: int | None = None
    allocated_devices: int = 0
    over_quota_enabled: bool = True


class PolicySnapshot(Snap):
    """One placement constraint, already resolved to the subject being placed.

    Attachment resolution is the caller's job: by the time a policy arrives here
    it applies. An empty allow-list means "unconstrained on this axis", which is
    what makes an all-empty policy a no-op rather than a total ban.
    """

    name: str
    enabled: bool = True
    allowed_regions: tuple[str, ...] = ()
    allowed_residencies: tuple[str, ...] = ()
    allowed_device_classes: tuple[str, ...] = ()
    allowed_clusters: tuple[str, ...] = ()
    denied_clusters: tuple[str, ...] = ()


class CapacitySnapshot(Snap):
    """Everything placement is allowed to look at, in one value."""

    clusters: tuple[ClusterSnapshot, ...] = ()
    quotas: tuple[QuotaSnapshot, ...] = ()
    policies: tuple[PolicySnapshot, ...] = ()


class PlacementRequest(Snap):
    """What is being placed, expressed as a device shape rather than a node count.

    `kind` carries the `service | task | gang` discriminator, and `device_count` is
    the whole indivisible unit this request stands for: one replica, one share of a
    split, or a gang's every rank.
    """

    workload: str
    project: str
    device_class: str
    device_count: int
    kind: str = "service"
    priority_value: int = 0
    preempts: bool = False
    device_memory_mib_min: int = 0
    requires_single_domain: bool = False
    domain_kind: str | None = None
    deployment_name: str | None = None
    atomic: bool = False

    @property
    def all_or_nothing(self) -> bool:
        """Whether this request must land whole in one cluster.

        A gang always must: its ranks are co-scheduled and a split gang is broken,
        not degraded. A tensor-parallel service replica is all-or-nothing too, but at
        the granularity of one replica — which is what a share is made of — so a
        service or task request is splittable across clusters and each resulting share
        is placed whole.
        """
        return self.atomic or self.kind == "gang"


class WorkloadDemand(Snap):
    """A workload's expansion inputs, with no ORM row anywhere in sight.

    `expand()` is the multi-cluster half of placement and has the same purity rule as
    `place()`, so the workload arrives here already resolved: the recipe's device shape
    per unit, the priority's numbers, and the declared totals. `unit_device_count` is
    what one replica (or one gang rank set, once multiplied by `gang_size`) costs, and
    every share is a whole number of units.

    `existing` is the current per-cluster shares keyed by cluster uid. It is what makes
    re-expansion stable: a scale-up fills the clusters already running before it reaches
    for a new one, so nothing reshuffles under a workload that is serving traffic.
    """

    workload: str
    project: str
    name: str
    device_class: str
    unit_device_count: int
    kind: str = "service"
    priority_value: int = 0
    preempts: bool = False
    device_memory_mib_min: int = 0
    requires_single_domain: bool = False
    domain_kind: str | None = None
    desired_scale: int = 1
    gang_size: int = 1
    strategy: str = "pack"
    cluster_floors: dict[str, int] = {}
    max_clusters: int | None = None
    pinned_cluster: str | None = None
    paused: bool = False
    existing: dict[str, int] = {}
