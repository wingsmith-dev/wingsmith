"""The placement function itself: pure, synchronous, and total.

It never raises for an unplaceable workload — an unplaceable workload is an
answer, not an error — and it never touches anything but its two arguments.
"""

import logging

from .decision import CLUSTER_PHASE
from .decision import POLICY_REGION
from .decision import QUOTA_EXHAUSTED
from .decision import POLICY_RESIDENCY
from .decision import CLUSTER_UNREACHABLE
from .decision import POLICY_DEVICE_CLASS
from .decision import NO_SUCH_DEVICE_CLASS
from .decision import INSUFFICIENT_CAPACITY
from .decision import INSUFFICIENT_TOPOLOGY
from .decision import POLICY_CLUSTER_DENIED
from .decision import POLICY_CLUSTER_NOT_ALLOWED
from .decision import Rejection
from .decision import ClusterVerdict
from .decision import PreemptionPlan
from .decision import PreemptionVictim
from .decision import PlacementDecision
from .snapshot import DeviceHold
from .snapshot import QuotaSnapshot
from .snapshot import DeviceSnapshot
from .snapshot import PolicySnapshot
from .snapshot import ClusterSnapshot
from .snapshot import CapacitySnapshot
from .snapshot import PlacementRequest
from .snapshot import TopologyDomainSnapshot


log = logging.getLogger(__name__)

PLACEABLE_PHASES = frozenset({"active"})
UNBOUNDED = 1 << 30


def place(request: PlacementRequest, snapshot: CapacitySnapshot) -> PlacementDecision:
    """Choose the cluster a workload goes to, or explain why none will do.

    Device selection is deliberately absent: which accelerators inside the winning
    cluster get used is a hot, local decision made by the in-cluster scheduler
    with node state at millisecond scale. This picks the cluster.
    """
    verdicts = tuple(sorted((_evaluate(request, c, snapshot) for c in snapshot.clusters), key=lambda v: v.cluster_name))
    feasible = [v for v in verdicts if v.feasible]
    if not feasible:
        return PlacementDecision(
            request=request.workload,
            placed=False,
            cluster=None,
            cluster_name=None,
            verdicts=verdicts,
            summary=_no_fit_summary(request, verdicts),
        )
    winner = min(feasible, key=_rank)
    log.debug("placed %s on %s (%d candidates)", request.workload, winner.cluster_name, len(feasible))
    return PlacementDecision(
        request=request.workload,
        placed=True,
        cluster=winner.cluster,
        cluster_name=winner.cluster_name,
        verdicts=verdicts,
        preemption=winner.preemption,
        summary=_fit_summary(request, winner),
    )


def _rank(verdict: ClusterVerdict) -> tuple:
    """The tiebreak, ordered so the same question always gets the same answer.

    Preemption-free wins first, then the cheapest eviction, then the tightest fit
    so large contiguous domains are kept whole, then the roomiest cluster, then
    the most quota headroom, and finally the cluster name — which guarantees
    determinism even when two clusters are identical in every other respect.
    """
    plan = verdict.preemption
    return (
        plan is not None and not plan.empty,
        plan.devices_reclaimed if plan else 0,
        len(plan.victims) if plan else 0,
        verdict.fit_slack,
        -verdict.free_devices,
        -(verdict.quota_headroom if verdict.quota_headroom is not None else UNBOUNDED),
        verdict.cluster_name,
    )


def _evaluate(request: PlacementRequest, cluster: ClusterSnapshot, snapshot: CapacitySnapshot) -> ClusterVerdict:
    reasons: list[Rejection] = []
    reasons.extend(_health_reasons(cluster))
    reasons.extend(_policy_reasons(request, cluster, snapshot.policies))

    candidates = tuple(
        d
        for d in cluster.devices
        if d.device_class == request.device_class and d.memory_mib >= request.device_memory_mib_min
    )
    free = tuple(d for d in candidates if d.free)
    quota = _quota_for(request, cluster, snapshot.quotas)
    headroom = _headroom(quota, request.device_count)
    if not candidates:
        reasons.append(
            Rejection(
                code=NO_SUCH_DEVICE_CLASS,
                message=(
                    f"cluster {cluster.name} has no {request.device_class} device with at least "
                    f"{request.device_memory_mib_min} MiB"
                ),
                detail={"device_class": request.device_class, "memory_mib_min": request.device_memory_mib_min},
            )
        )
    if headroom is not None and headroom < request.device_count:
        reasons.append(
            Rejection(
                code=QUOTA_EXHAUSTED,
                message=(
                    f"project {request.project} has {headroom} {request.device_class} left under quota in "
                    f"{cluster.name}, {request.device_count} requested"
                ),
                detail={
                    "headroom_devices": headroom,
                    "requested_devices": request.device_count,
                    "allocated_devices": quota.allocated_devices if quota else 0,
                    "over_quota_enabled": quota.over_quota_enabled if quota else True,
                },
            )
        )

    fit = _fit(request, cluster, candidates, free)
    reasons.extend(fit.reasons)
    return ClusterVerdict(
        cluster=cluster.uid,
        cluster_name=cluster.name,
        feasible=not reasons,
        free_devices=len(free),
        reasons=tuple(reasons),
        domain=fit.domain,
        domain_identifier=fit.domain_identifier,
        domain_free_devices=fit.domain_free,
        largest_domain_free_devices=fit.largest_domain_free,
        fit_slack=fit.slack,
        quota_headroom=headroom,
        preemption=fit.plan,
    )


def _health_reasons(cluster: ClusterSnapshot) -> list[Rejection]:
    """Declared phase and observed reachability, checked as the two axes they are."""
    reasons: list[Rejection] = []
    if cluster.phase not in PLACEABLE_PHASES:
        reasons.append(
            Rejection(
                code=CLUSTER_PHASE,
                message=f"cluster {cluster.name} is {cluster.phase}, not active",
                detail={"phase": cluster.phase},
            )
        )
    if not cluster.reachable:
        reasons.append(
            Rejection(
                code=CLUSTER_UNREACHABLE,
                message=f"cluster {cluster.name} has no live agent session",
                detail={"phase": cluster.phase, "reachable": False},
            )
        )
    return reasons


def _policy_reasons(
    request: PlacementRequest, cluster: ClusterSnapshot, policies: tuple[PolicySnapshot, ...]
) -> list[Rejection]:
    reasons: list[Rejection] = []
    for policy in policies:
        if not policy.enabled:
            continue
        if policy.allowed_regions and cluster.region not in policy.allowed_regions:
            reasons.append(
                Rejection(
                    code=POLICY_REGION,
                    message=f"policy {policy.name} does not allow region {cluster.region}",
                    detail={"policy": policy.name, "allowed_regions": list(policy.allowed_regions)},
                )
            )
        if policy.allowed_residencies and cluster.residency not in policy.allowed_residencies:
            reasons.append(
                Rejection(
                    code=POLICY_RESIDENCY,
                    message=f"policy {policy.name} does not allow residency {cluster.residency}",
                    detail={"policy": policy.name, "allowed_residencies": list(policy.allowed_residencies)},
                )
            )
        if policy.allowed_device_classes and request.device_class not in policy.allowed_device_classes:
            reasons.append(
                Rejection(
                    code=POLICY_DEVICE_CLASS,
                    message=f"policy {policy.name} does not allow device class {request.device_class}",
                    detail={"policy": policy.name, "allowed_device_classes": list(policy.allowed_device_classes)},
                )
            )
        if policy.allowed_clusters and cluster.uid not in policy.allowed_clusters:
            reasons.append(
                Rejection(
                    code=POLICY_CLUSTER_NOT_ALLOWED,
                    message=f"policy {policy.name} restricts placement to other clusters",
                    detail={"policy": policy.name, "allowed_clusters": list(policy.allowed_clusters)},
                )
            )
        if cluster.uid in policy.denied_clusters:
            reasons.append(
                Rejection(
                    code=POLICY_CLUSTER_DENIED,
                    message=f"policy {policy.name} denies cluster {cluster.name}",
                    detail={"policy": policy.name},
                )
            )
    return reasons


def _quota_for(
    request: PlacementRequest, cluster: ClusterSnapshot, quotas: tuple[QuotaSnapshot, ...]
) -> QuotaSnapshot | None:
    """The narrowest quota that applies: cluster-scoped beats global."""
    scoped = None
    globalish = None
    for quota in quotas:
        if quota.project != request.project or quota.device_class != request.device_class:
            continue
        if quota.cluster == cluster.uid:
            scoped = quota
        elif quota.cluster is None:
            globalish = quota
    return scoped or globalish


def _headroom(quota: QuotaSnapshot | None, requested: int) -> int | None:
    """Devices the project may still take, or None when nothing caps it."""
    if quota is None:
        return None
    cap = quota.limit_devices
    if not quota.over_quota_enabled:
        cap = quota.guaranteed_devices if cap is None else min(cap, quota.guaranteed_devices)
    if cap is None:
        return None
    return max(cap - quota.allocated_devices, 0)


class _Fit:
    """The capacity half of a verdict, computed once and read into the struct."""

    def __init__(self) -> None:
        self.reasons: list[Rejection] = []
        self.domain: str | None = None
        self.domain_identifier: str | None = None
        self.domain_free: int = 0
        self.largest_domain_free: int = 0
        self.slack: int = 0
        self.plan: PreemptionPlan | None = None


def _fit(
    request: PlacementRequest,
    cluster: ClusterSnapshot,
    candidates: tuple[DeviceSnapshot, ...],
    free: tuple[DeviceSnapshot, ...],
) -> _Fit:
    if request.requires_single_domain:
        return _fit_contiguous(request, cluster, candidates, free)
    return _fit_aggregate(request, cluster, candidates, free)


def _fit_aggregate(
    request: PlacementRequest,
    cluster: ClusterSnapshot,
    candidates: tuple[DeviceSnapshot, ...],
    free: tuple[DeviceSnapshot, ...],
) -> _Fit:
    fit = _Fit()
    domains = {d.uid: d for d in cluster.domains}
    fit.largest_domain_free = max(
        (len(g) for g in _group_by_domain(free, domains, request.domain_kind).values()), default=0
    )
    needed = request.device_count
    if len(free) >= needed:
        fit.slack = len(free) - needed
        return fit
    plan = _plan_preemption(request, candidates, needed - len(free))
    if plan is not None:
        fit.plan = plan
        fit.slack = len(free) + plan.devices_reclaimed - needed
        return fit
    fit.reasons.append(
        Rejection(
            code=INSUFFICIENT_CAPACITY,
            message=(
                f"cluster {cluster.name} has {len(free)} free {request.device_class}, "
                f"{needed} requested{_atomic_note(request)}"
            ),
            detail={
                "free_devices": len(free),
                "requested_devices": needed,
                "reclaimable_devices": len(_reclaimable(request, candidates)),
            },
        )
    )
    return fit


def _fit_contiguous(
    request: PlacementRequest,
    cluster: ClusterSnapshot,
    candidates: tuple[DeviceSnapshot, ...],
    free: tuple[DeviceSnapshot, ...],
) -> _Fit:
    """A gang lands inside one topology domain or it does not land.

    Aggregate capacity is not an answer here: eight devices spread over four
    nodes cannot serve a model that needs one NVLink fabric, which is why the
    rejection reports the largest domain rather than the cluster total.
    """
    fit = _Fit()
    domains = {d.uid: d for d in cluster.domains}
    free_by_domain = _group_by_domain(free, domains, request.domain_kind)
    all_by_domain = _group_by_domain(candidates, domains, request.domain_kind)
    fit.largest_domain_free = max((len(g) for g in free_by_domain.values()), default=0)
    needed = request.device_count

    exact = sorted(
        (len(free_by_domain.get(uid, ())), uid) for uid in all_by_domain if len(free_by_domain.get(uid, ())) >= needed
    )
    if exact:
        size, uid = exact[0]
        fit.domain = uid
        fit.domain_identifier = domains[uid].identifier if uid in domains else uid
        fit.domain_free = size
        fit.slack = size - needed
        return fit

    best: tuple[tuple, str, PreemptionPlan] | None = None
    for uid, group in sorted(all_by_domain.items()):
        shortfall = needed - len(free_by_domain.get(uid, ()))
        plan = _plan_preemption(request, tuple(group), shortfall)
        if plan is None:
            continue
        key = (plan.devices_reclaimed, len(plan.victims), uid)
        if best is None or key < best[0]:
            best = (key, uid, plan)
    if best is not None:
        _, uid, plan = best
        fit.domain = uid
        fit.domain_identifier = domains[uid].identifier if uid in domains else uid
        fit.domain_free = len(free_by_domain.get(uid, ()))
        fit.plan = plan
        fit.slack = fit.domain_free + plan.devices_reclaimed - needed
        return fit

    kind = request.domain_kind or "topology"
    fit.reasons.append(
        Rejection(
            code=INSUFFICIENT_TOPOLOGY,
            message=(
                f"cluster {cluster.name} has {fit.largest_domain_free} free {request.device_class} in its largest "
                f"{kind} domain, {needed} requested contiguously ({len(free)} free in the cluster overall)"
            ),
            detail={
                "largest_domain_free_devices": fit.largest_domain_free,
                "free_devices": len(free),
                "requested_devices": needed,
                "domain_kind": request.domain_kind,
            },
        )
    )
    return fit


def _group_by_domain(
    devices: tuple[DeviceSnapshot, ...],
    domains: dict[str, TopologyDomainSnapshot],
    kind: str | None,
) -> dict[str, list[DeviceSnapshot]]:
    """Bucket devices by the domain of the requested kind that contains them.

    Domains nest, so a device attached to an NVLink island is also inside its
    PCIe root and its NUMA node; asking for one kind walks up the parent chain
    rather than matching only the leaf.
    """
    grouped: dict[str, list[DeviceSnapshot]] = {}
    for device in devices:
        uid = _domain_of(device.domain, domains, kind)
        if uid is None:
            continue
        grouped.setdefault(uid, []).append(device)
    return grouped


def _domain_of(uid: str | None, domains: dict[str, TopologyDomainSnapshot], kind: str | None) -> str | None:
    seen: set[str] = set()
    while uid is not None and uid not in seen:
        seen.add(uid)
        domain = domains.get(uid)
        if domain is None:
            return None
        if kind is None or domain.kind == kind:
            return domain.uid
        uid = domain.parent
    return None


def _reclaimable(request: PlacementRequest, devices: tuple[DeviceSnapshot, ...]) -> list[DeviceSnapshot]:
    if not request.preempts:
        return []
    return [
        d
        for d in devices
        if d.healthy
        and d.allocatable
        and d.held_by is not None
        and d.held_by.preemptible
        and d.held_by.priority_value < request.priority_value
    ]


def _plan_preemption(
    request: PlacementRequest, devices: tuple[DeviceSnapshot, ...], shortfall: int
) -> PreemptionPlan | None:
    """Name the workloads whose eviction would make this fit, cheapest first.

    Victims are whole workloads, never individual devices: an allocation is the
    unit of commitment, so taking one device from a gang costs the whole gang and
    the plan says so.
    """
    if shortfall <= 0:
        return None
    scope = _reclaimable(request, devices)
    if not scope:
        return None
    in_scope: dict[str, tuple[DeviceHold, list[DeviceSnapshot]]] = {}
    for device in scope:
        hold = device.held_by
        if hold is None:
            continue
        in_scope.setdefault(hold.workload, (hold, []))[1].append(device)
    ordered = sorted(in_scope.items(), key=lambda item: (item[1][0].priority_value, len(item[1][1]), item[0]))
    victims: list[PreemptionVictim] = []
    reclaimed = 0
    for workload, (hold, held) in ordered:
        if reclaimed >= shortfall:
            break
        victims.append(
            PreemptionVictim(
                workload=workload,
                project=hold.project,
                priority_value=hold.priority_value,
                devices_in_scope=len(held),
                devices_released=len(held),
            )
        )
        reclaimed += len(held)
    if reclaimed < shortfall:
        return None
    return PreemptionPlan(victims=tuple(victims), devices_reclaimed=reclaimed)


def _atomic_note(request: PlacementRequest) -> str:
    return " (all or nothing)" if request.all_or_nothing else ""


def _fit_summary(request: PlacementRequest, winner: ClusterVerdict) -> str:
    if winner.preemption is not None and not winner.preemption.empty:
        names = ", ".join(v.workload for v in winner.preemption.victims)
        return (
            f"{request.workload} fits on {winner.cluster_name} only if {names} "
            f"({winner.preemption.devices_reclaimed} devices) are preempted"
        )
    return (
        f"{request.workload} placed on {winner.cluster_name}: "
        f"{request.device_count} {request.device_class} of {winner.free_devices} free"
    )


def _no_fit_summary(request: PlacementRequest, verdicts: tuple[ClusterVerdict, ...]) -> str:
    if not verdicts:
        return f"{request.workload} not placed: no clusters in the snapshot"
    parts = [f"{v.cluster_name}: {v.reasons[0].message}" for v in verdicts if v.reasons]
    return f"{request.workload} not placed; " + "; ".join(parts)
