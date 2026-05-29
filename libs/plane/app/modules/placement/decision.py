"""The output side of placement: a decision that explains itself per cluster.

A scheduler that answers "no capacity" is unoperable. Every cluster considered
gets a verdict carrying the numbers the rejection was computed from, so the
answer to "why not iad-1" is in the return value rather than in a log line
someone has to go and find.
"""

from msgspec import Struct


class Verdict(Struct, frozen=True, kw_only=True):
    """Base for every decision struct: immutable and keyword-constructed."""


CLUSTER_PHASE = "cluster_phase"
CLUSTER_UNREACHABLE = "cluster_unreachable"
POLICY_REGION = "policy_region"
POLICY_RESIDENCY = "policy_residency"
POLICY_DEVICE_CLASS = "policy_device_class"
POLICY_CLUSTER_NOT_ALLOWED = "policy_cluster_not_allowed"
POLICY_CLUSTER_DENIED = "policy_cluster_denied"
QUOTA_EXHAUSTED = "quota_exhausted"
NO_SUCH_DEVICE_CLASS = "no_such_device_class"
INSUFFICIENT_CAPACITY = "insufficient_capacity"
INSUFFICIENT_TOPOLOGY = "insufficient_topology"
TENSOR_GROUP_TOO_LARGE = "tensor_group_too_large"
NAME_TAKEN = "name_taken"


class Rejection(Verdict):
    """One reason a cluster was ruled out, with the numbers behind it."""

    code: str
    message: str
    detail: dict = {}


class PreemptionVictim(Verdict):
    """A workload that would have to go for this placement to fit.

    Preemption reasons per-cluster, so `deployment` names the victim's copy in this
    cluster and `workload` the global object it belongs to. Evicting "a workload"
    from a cluster is always evicting its deployment there.

    `devices_in_scope` is what preempting it contributes here; `devices_released`
    is everything it holds in the cluster. They differ when a workload straddles
    topology domains, and the difference is the honest cost of the eviction —
    an allocation dies whole, so the devices outside the scope are freed too.
    """

    workload: str
    project: str
    priority_value: int
    devices_in_scope: int
    devices_released: int
    deployment: str | None = None


class PreemptionPlan(Verdict):
    """What would be evicted, identified but never performed.

    Placement returns this and stops. Actually preempting is a caller's decision,
    taken with a transaction open, which is exactly the thing this package does
    not have.
    """

    victims: tuple[PreemptionVictim, ...]
    devices_reclaimed: int

    @property
    def empty(self) -> bool:
        return not self.victims


class ClusterVerdict(Verdict):
    """One cluster's full reasoning, feasible or not.

    Carries the counts even when it fails, because "6 free h100 in the largest
    nvlink domain, 8 asked for" is an operator's next action and "no capacity"
    is not.
    """

    cluster: str
    cluster_name: str
    feasible: bool
    free_devices: int
    reserved_devices: int = 0
    """Devices this cluster already owes a deployment that has not bound them yet.

    Reported beside `free_devices` rather than folded into it, because the two are
    different kinds of fact and an operator reading "0 free" when eight accelerators
    are visibly idle has been told something untrue. Free is observed; reserved is
    promised; `available_devices` is what is actually on offer.
    """
    available_devices: int = 0
    """`free_devices` net of `reserved_devices`. The number every fit is decided on."""
    reasons: tuple[Rejection, ...] = ()
    domain: str | None = None
    domain_identifier: str | None = None
    domain_free_devices: int = 0
    largest_domain_free_devices: int = 0
    fit_slack: int = 0
    quota_headroom: int | None = None
    preemption: PreemptionPlan | None = None


class PlacementDecision(Verdict):
    """Which cluster, and why every other one lost.

    `placed` is false and `cluster` is None when nothing fits; the verdicts are
    populated identically either way, so the rejection path is as informative as
    the success path.
    """

    request: str
    placed: bool
    cluster: str | None
    cluster_name: str | None
    verdicts: tuple[ClusterVerdict, ...]
    preemption: PreemptionPlan | None = None
    summary: str = ""

    @property
    def requires_preemption(self) -> bool:
        return self.preemption is not None and not self.preemption.empty


class Share(Verdict):
    """One cluster's slice of a workload's total scale.

    `replicas` is counted in the workload's own unit — replicas for a service, ranks
    for a gang, completions for a task — and is always whole. It is this cluster's
    share and never the total; summing a share against a total is always a bug.
    """

    cluster: str
    cluster_name: str
    replicas: int


class ExpansionDecision(Verdict):
    """How a workload's total scale was spread over clusters, and why.

    `shares` is the answer, `verdicts` is the reasoning for every cluster considered,
    and `shortfall` is what could not be placed anywhere. A shortfall is a partial
    placement rather than a failure: the shares that were found are real, and the
    workload's rollup shows placed below desired until capacity appears.

    `splittable` is false for a gang, whose ranks are co-scheduled. A gang that fits
    nowhere whole comes back with no shares and `shortfall == gang_size`; there is no
    degraded answer to fall back on, and the caller turns that into a hard rejection.
    """

    workload: str
    shares: tuple[Share, ...]
    verdicts: tuple[ClusterVerdict, ...]
    desired: int
    shortfall: int
    splittable: bool = True
    summary: str = ""

    @property
    def placed(self) -> int:
        return sum(s.replicas for s in self.shares)
