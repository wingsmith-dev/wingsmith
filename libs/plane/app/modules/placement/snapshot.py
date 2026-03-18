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


class DeviceSnapshot(Snap):
    """One accelerator, as observed. The unit every capacity number is counted in.

    A device is free when it is healthy, allocatable, and unheld — three separate
    facts, because a device can be idle and still unusable.
    """

    uid: str
    node: str
    device_class: str
    memory_mib: int = 0
    healthy: bool = True
    allocatable: bool = True
    domain: str | None = None
    held_by: DeviceHold | None = None

    @property
    def free(self) -> bool:
        return self.healthy and self.allocatable and self.held_by is None


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

    `kind` carries the `service | task | gang` discriminator. Atomicity is not
    derived from it: a tensor-parallel service is every bit as all-or-nothing as a
    gang job, so `atomic` defaults true and `kind` only forces it.
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
    atomic: bool = True

    @property
    def all_or_nothing(self) -> bool:
        return self.atomic or self.kind == "gang"
