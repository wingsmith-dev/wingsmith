"""The placement function itself: pure, synchronous, and total.

It never raises for an unplaceable workload — an unplaceable workload is an
answer, not an error — and it never touches anything but its two arguments.
"""

import logging

from msgspec import structs

from .decision import NAME_TAKEN
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
from .decision import TENSOR_GROUP_TOO_LARGE
from .decision import POLICY_CLUSTER_NOT_ALLOWED
from .decision import Share
from .decision import Rejection
from .decision import ClusterVerdict
from .decision import PreemptionPlan
from .decision import PreemptionVictim
from .decision import ExpansionDecision
from .decision import PlacementDecision
from .snapshot import DeviceHold
from .snapshot import ReservedUnit
from .snapshot import QuotaSnapshot
from .snapshot import DeviceSnapshot
from .snapshot import PolicySnapshot
from .snapshot import WorkloadDemand
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

    Preemption-free wins first, then the cheapest eviction, then the tightest
    domain so large fabrics are kept whole, then the roomiest cluster, then the
    most quota headroom, and finally the cluster name — which guarantees
    determinism even when two clusters are identical in every other respect.

    Roomiest is measured on `available_devices` and not on `free_devices`, and that
    one word is what stops two workloads created back to back from landing on the
    same cluster. Ranked on free devices, the second placement sees a cluster that
    the first has already been promised and reads it as the emptiest one on offer.

    Tightness is scoped to a domain, and only to a domain. `fit_slack` measures
    the leftover in whatever the fit was made against: one topology domain for a
    gang, the whole cluster for anything else. Ranking on the second sorts by
    free devices ascending, so the fullest cluster that still fits always wins —
    best-fit packing, which would drain one cluster before touching the next and
    make the roomiest-cluster key below unreachable. A gang still wants the
    smallest fabric that holds it, so the key stays for a domain fit and is
    dropped for an aggregate one.
    """
    plan = verdict.preemption
    return (
        plan is not None and not plan.empty,
        plan.devices_reclaimed if plan else 0,
        len(plan.victims) if plan else 0,
        verdict.fit_slack if verdict.domain else 0,
        -verdict.available_devices,
        -(verdict.quota_headroom if verdict.quota_headroom is not None else UNBOUNDED),
        verdict.cluster_name,
    )


def _evaluate(request: PlacementRequest, cluster: ClusterSnapshot, snapshot: CapacitySnapshot) -> ClusterVerdict:
    reasons: list[Rejection] = []
    reasons.extend(_health_reasons(cluster))
    reasons.extend(_policy_reasons(request, cluster, snapshot.policies))
    reasons.extend(_name_reasons(request, cluster))

    candidates = tuple(
        d
        for d in cluster.devices
        if d.device_class == request.device_class and d.memory_mib >= request.device_memory_mib_min
    )
    free = tuple(d for d in candidates if d.free)
    supply = _Supply(candidates, free, _owed(request, cluster))
    quota = _quota_for(request, cluster, snapshot.quotas)
    headroom = _headroom(quota, _quota_reserved(request, cluster, quota, snapshot))
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

    fit = _fit(request, cluster, supply)
    reasons.extend(fit.reasons)
    return ClusterVerdict(
        cluster=cluster.uid,
        cluster_name=cluster.name,
        feasible=not reasons,
        free_devices=len(free),
        reserved_devices=supply.reserved_devices,
        available_devices=supply.available,
        reasons=tuple(reasons),
        domain=fit.domain,
        domain_identifier=fit.domain_identifier,
        domain_free_devices=fit.domain_free,
        largest_domain_free_devices=fit.largest_domain_free,
        fit_slack=fit.slack,
        quota_headroom=headroom,
        preemption=fit.plan,
    )


def _name_reasons(request: PlacementRequest, cluster: ClusterSnapshot) -> list[Rejection]:
    """A deployment name is unique per cluster, so a taken one rules the cluster out.

    Two deployments of the same workload in two clusters share a name and do not
    collide; a name held by a *different* workload does, and placement rejects the
    cluster rather than inventing a suffix that would then be the name nobody typed.
    """
    if request.deployment_name is None:
        return []
    owner = cluster.taken_names.get(request.deployment_name)
    if owner is None or owner == request.workload:
        return []
    return [
        Rejection(
            code=NAME_TAKEN,
            message=f"cluster {cluster.name} already has a deployment named {request.deployment_name!r}",
            detail={"name": request.deployment_name, "held_by": owner},
        )
    ]


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


def _headroom(quota: QuotaSnapshot | None, reserved: int) -> int | None:
    """Devices the project may still take, or None when nothing caps it.

    `allocated_devices` is the observed draw and `reserved` is the declared one that
    has not been observed yet. Both come off the cap, because a quota that only
    counts what a cluster has confirmed lets a project spend the same devices twice —
    the same double-spend `_owed` closes for capacity, asked of the ceiling instead of
    the fleet.
    """
    if quota is None:
        return None
    cap = quota.limit_devices
    if not quota.over_quota_enabled:
        cap = quota.guaranteed_devices if cap is None else min(cap, quota.guaranteed_devices)
    if cap is None:
        return None
    return max(cap - quota.allocated_devices - reserved, 0)


def _owed(request: PlacementRequest, cluster: ClusterSnapshot) -> tuple[ReservedUnit, ...]:
    """What this cluster already owes somebody other than the caller.

    A workload's own reservation is dropped, and that exclusion is what makes
    re-expansion stable. Scaling a workload from four replicas to six re-runs the
    same expansion over the same clusters; counting its own four pending replicas
    against it would read its home cluster as full, move the share somewhere else,
    and reshuffle a service that is in the middle of coming up. A workload never
    competes with itself for capacity it has already been given.
    """
    return tuple(unit for unit in cluster.reserved if unit.workload != request.workload)


def _quota_reserved(
    request: PlacementRequest,
    cluster: ClusterSnapshot,
    quota: QuotaSnapshot | None,
    snapshot: CapacitySnapshot,
) -> int:
    """The project's own pending draw, over the scope its quota actually covers.

    Scope is the whole reason this is not just `cluster.reserved`. A cluster-scoped
    quota is spent in one cluster and a global one is spent everywhere, so a global
    quota has to see every cluster's reservations or it caps a project against a
    third of what it has already asked for.

    The caller's own workload is excluded here for the same reason it is in `_owed`:
    a scale-up would otherwise be charged for the replicas it already has.
    """
    if quota is None:
        return 0
    scope = snapshot.clusters if quota.cluster is None else (cluster,)
    return sum(
        unit.devices
        for c in scope
        for unit in c.reserved
        if unit.project == request.project and unit.workload != request.workload
    )


class _Supply:
    """What a cluster has to offer this request, net of what it already owes.

    Free and reserved are carried side by side rather than merged, because they are
    not the same kind of number. A free device has an address: it can be named,
    grouped by fabric, and preempted. A reservation has a count and a shape and no
    address at all, because the cluster has not yet said which accelerators it will
    bind. `available` is the only figure a fit is decided on; the other two are what
    the operator is shown so the answer is legible.
    """

    def __init__(
        self,
        candidates: tuple[DeviceSnapshot, ...],
        free: tuple[DeviceSnapshot, ...],
        reserved: tuple[ReservedUnit, ...],
    ) -> None:
        self.candidates = candidates
        self.free = free
        self.reserved = reserved
        self.reserved_devices = sum(unit.devices for unit in reserved)
        self.available = max(len(free) - self.reserved_devices, 0)


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


def _fit(request: PlacementRequest, cluster: ClusterSnapshot, supply: _Supply) -> _Fit:
    too_wide = _tensor_group_too_wide(request, cluster, supply.candidates)
    if too_wide is not None:
        fit = _Fit()
        fit.reasons.append(too_wide)
        return fit
    if request.requires_single_domain:
        return _fit_contiguous(request, cluster, supply)
    return _fit_aggregate(request, cluster, supply)


def _tensor_group_too_wide(
    request: PlacementRequest,
    cluster: ClusterSnapshot,
    candidates: tuple[DeviceSnapshot, ...],
) -> Rejection | None:
    """Rules out a cluster whose machines are too small to hold one tensor group.

    Counted over every candidate device rather than the free ones, because this is a
    fact about the hardware and not about how busy it is. A cluster of one-device
    machines does not become able to run a tensor-parallel-two replica when something
    else finishes, so reporting it as a capacity shortfall would send someone to wait
    for a queue that will never clear — and preemption would be offered as the remedy
    for a machine that is simply the wrong shape.

    The same judgement the operator makes in `reconcile.schedulable`, made here for the
    reason `DeviceSnapshot` already gives for duplicating `nodeUsable`: a plane that
    ignores it picks a cluster that cluster will refuse, and the workload sits pending
    against a message about the device plugin.
    """
    want = max(request.tensor_parallel, 1)
    if want < 2:
        return None
    per_node: dict[str, int] = {}
    for device in candidates:
        per_node[device.node] = per_node.get(device.node, 0) + 1
    largest = max(per_node.values(), default=0)
    if largest >= want:
        return None
    return Rejection(
        code=TENSOR_GROUP_TOO_LARGE,
        message=(
            f"cluster {cluster.name} has {largest} {request.device_class} on its largest node, "
            f"{want} needed in one node for a tensor group"
        ),
        detail={
            "largest_node_devices": largest,
            "tensor_parallel": want,
            "requested_devices": request.device_count,
        },
    )


def _fit_aggregate(request: PlacementRequest, cluster: ClusterSnapshot, supply: _Supply) -> _Fit:
    fit = _Fit()
    domains = {d.uid: d for d in cluster.domains}
    fit.largest_domain_free = max(
        _burn(_group_by_domain(supply.free, domains, request.domain_kind), supply.reserved).values(), default=0
    )
    needed = request.device_count
    if supply.available >= needed:
        fit.slack = supply.available - needed
        return fit
    plan = _plan_preemption(request, supply.candidates, needed - supply.available)
    if plan is not None:
        fit.plan = plan
        fit.slack = supply.available + plan.devices_reclaimed - needed
        return fit
    fit.reasons.append(
        Rejection(
            code=INSUFFICIENT_CAPACITY,
            message=(
                f"cluster {cluster.name} has {supply.available} available {request.device_class}"
                f"{_owed_note(supply)}, {needed} requested{_atomic_note(request)}"
            ),
            detail={
                "free_devices": len(supply.free),
                "reserved_devices": supply.reserved_devices,
                "available_devices": supply.available,
                "requested_devices": needed,
                "reclaimable_devices": len(_reclaimable(request, supply.candidates)),
            },
        )
    )
    return fit


def _fit_contiguous(request: PlacementRequest, cluster: ClusterSnapshot, supply: _Supply) -> _Fit:
    """A gang lands inside one topology domain or it does not land.

    Aggregate capacity is not an answer here: eight devices spread over four
    nodes cannot serve a model that needs one NVLink fabric, which is why the
    rejection reports the largest domain rather than the cluster total.

    Both constraints have to hold once reservations are in play, and they can fail
    independently. A fabric can look big enough while the cluster as a whole is
    already spoken for by pending replicas that will spread across it, so the domain
    check runs against the burnt-down per-domain counts and the cluster total is
    checked separately — a request that clears one and not the other is rejected for
    the one it actually failed.
    """
    fit = _Fit()
    domains = {d.uid: d for d in cluster.domains}
    free_by_domain = _group_by_domain(supply.free, domains, request.domain_kind)
    all_by_domain = _group_by_domain(supply.candidates, domains, request.domain_kind)
    remaining = _burn(free_by_domain, supply.reserved)
    fit.largest_domain_free = max(remaining.values(), default=0)
    needed = request.device_count

    if supply.available >= needed:
        exact = sorted((remaining.get(uid, 0), uid) for uid in all_by_domain if remaining.get(uid, 0) >= needed)
        if exact:
            size, uid = exact[0]
            fit.domain = uid
            fit.domain_identifier = domains[uid].identifier if uid in domains else uid
            fit.domain_free = size
            fit.slack = size - needed
            return fit

    best: tuple[tuple, str, PreemptionPlan] | None = None
    for uid, group in sorted(all_by_domain.items()):
        shortfall = needed - remaining.get(uid, 0)
        plan = _plan_preemption(request, tuple(group), shortfall)
        if plan is None or supply.available + plan.devices_reclaimed < needed:
            continue
        key = (plan.devices_reclaimed, len(plan.victims), uid)
        if best is None or key < best[0]:
            best = (key, uid, plan)
    if best is not None:
        _, uid, plan = best
        fit.domain = uid
        fit.domain_identifier = domains[uid].identifier if uid in domains else uid
        fit.domain_free = remaining.get(uid, 0)
        fit.plan = plan
        fit.slack = fit.domain_free + plan.devices_reclaimed - needed
        return fit

    if supply.available < needed:
        fit.reasons.append(
            Rejection(
                code=INSUFFICIENT_CAPACITY,
                message=(
                    f"cluster {cluster.name} has {supply.available} available {request.device_class}"
                    f"{_owed_note(supply)}, {needed} requested{_atomic_note(request)}"
                ),
                detail={
                    "free_devices": len(supply.free),
                    "reserved_devices": supply.reserved_devices,
                    "available_devices": supply.available,
                    "requested_devices": needed,
                    "reclaimable_devices": len(_reclaimable(request, supply.candidates)),
                },
            )
        )
        return fit

    kind = request.domain_kind or "topology"
    fit.reasons.append(
        Rejection(
            code=INSUFFICIENT_TOPOLOGY,
            message=(
                f"cluster {cluster.name} has {fit.largest_domain_free} available {request.device_class} in its largest "
                f"{kind} domain, {needed} requested contiguously "
                f"({supply.available} available in the cluster overall{_owed_note(supply)})"
            ),
            detail={
                "largest_domain_free_devices": fit.largest_domain_free,
                "free_devices": len(supply.free),
                "reserved_devices": supply.reserved_devices,
                "available_devices": supply.available,
                "requested_devices": needed,
                "domain_kind": request.domain_kind,
            },
        )
    )
    return fit


def _burn(
    free_by_domain: dict[str, list[DeviceSnapshot]],
    reserved: tuple[ReservedUnit, ...],
) -> dict[str, int]:
    """Charge every pending fabric-bound unit to the roomiest domain still standing.

    A reservation names no device, so which fabric a pending gang will take is not
    knowable from here — the cluster has not chosen yet, and under rule 3 it is not
    ours to choose. Largest-first is the pessimistic reading and the only safe one:
    if the biggest free fabric might be the one already spoken for, a gang that would
    exactly fill it has to be told no. The alternative is placing it onto a cluster
    that refuses it the moment the earlier deployment binds, which is the bounce this
    whole mechanism exists to stop.

    Units that need no fabric are deliberately not charged to one. They spread, so
    they come off the cluster total in `_Supply` instead; charging them here as well
    would spend the same devices twice and empty a cluster with room to spare.

    Ties are broken on the domain uid so the same snapshot always burns the same
    fabrics, which is what keeps two identical questions getting the same answer.
    """
    remaining = {uid: len(group) for uid, group in free_by_domain.items()}
    if not remaining:
        return remaining
    bound = sorted((u for u in reserved if u.requires_single_domain), key=lambda u: (-u.devices, u.deployment or ""))
    for unit in bound:
        uid = max(remaining, key=lambda k: (remaining[k], k))
        remaining[uid] = max(remaining[uid] - unit.devices, 0)
    return remaining


def _owed_note(supply: _Supply) -> str:
    """Names the promise, so "0 available" never reads as "0 idle"."""
    if supply.reserved_devices <= 0:
        return ""
    return f" ({len(supply.free)} free, {supply.reserved_devices} reserved by deployments not yet bound)"


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
        in_scope.setdefault(hold.deployment or hold.workload, (hold, []))[1].append(device)
    ordered = sorted(in_scope.items(), key=lambda item: (item[1][0].priority_value, len(item[1][1]), item[0]))
    victims: list[PreemptionVictim] = []
    reclaimed = 0
    for _, (hold, held) in ordered:
        if reclaimed >= shortfall:
            break
        victims.append(
            PreemptionVictim(
                workload=hold.workload,
                deployment=hold.deployment,
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
    owed = f" ({winner.reserved_devices} reserved elsewhere)" if winner.reserved_devices else ""
    return (
        f"{request.workload} placed on {winner.cluster_name}: "
        f"{request.device_count} {request.device_class} of {winner.available_devices} available{owed}"
    )


def _no_fit_summary(request: PlacementRequest, verdicts: tuple[ClusterVerdict, ...]) -> str:
    if not verdicts:
        return f"{request.workload} not placed: no clusters in the snapshot"
    parts = [f"{v.cluster_name}: {v.reasons[0].message}" for v in verdicts if v.reasons]
    return f"{request.workload} not placed; " + "; ".join(parts)


def expand(demand: WorkloadDemand, snapshot: CapacitySnapshot) -> ExpansionDecision:
    """Spread a workload's total scale over the clusters that can take it.

    The multi-cluster half of placement, and pure for the same reason `place()` is:
    it reads a snapshot and returns an answer, and the caller persists it. Every
    cluster is evaluated with the existing policy, health, quota and topology rules
    against ONE unit of the workload, and the split is then a question of how many
    whole units each surviving cluster can hold.

    Splittability is a property of `kind` and is the one thing this branches on:
    `service` and `task` fan out, `gang` is atomic and comes back with a single
    share or with none at all. Nothing below a workload is entitled to read `kind`
    (`CLAUDE.md` rule on the supertype); expansion is above it.

    `pack` is the default because it makes an existing single-cluster workload
    re-expand onto the same single cluster: spreading is a thing you ask for, not
    something a control plane does to a running service on its own.
    """
    if demand.kind == "gang":
        return _expand_gang(demand, snapshot)

    unit = _unit_request(demand)
    verdicts = tuple(sorted((_evaluate(unit, c, snapshot) for c in snapshot.clusters), key=lambda v: v.cluster_name))
    by_uid = {v.cluster: v for v in verdicts}
    desired = max(demand.desired_scale, 0)

    if demand.paused:
        held = tuple(
            Share(cluster=v.cluster, cluster_name=v.cluster_name, replicas=0) for v in _existing(demand, by_uid)
        )
        return ExpansionDecision(
            workload=demand.workload,
            shares=held,
            verdicts=verdicts,
            desired=0,
            shortfall=0,
            summary=f"{demand.workload} is paused; every share is 0 and no deployment is removed",
        )

    capacity = _capacity(demand, verdicts)
    assigned: dict[str, int] = {}
    remaining = _assign_floors(demand, by_uid, capacity, assigned, desired)
    remaining = _fill(demand, by_uid, capacity, assigned, remaining)

    shares = tuple(
        Share(cluster=uid, cluster_name=by_uid[uid].cluster_name, replicas=count)
        for uid, count in sorted(assigned.items(), key=lambda item: by_uid[item[0]].cluster_name)
    )
    return ExpansionDecision(
        workload=demand.workload,
        shares=shares,
        verdicts=verdicts,
        desired=desired,
        shortfall=remaining,
        summary=_expansion_summary(demand, shares, remaining, verdicts),
    )


def _expand_gang(demand: WorkloadDemand, snapshot: CapacitySnapshot) -> ExpansionDecision:
    """A gang lands whole in one cluster or it does not land.

    Its ranks are co-scheduled, so a split gang is broken rather than degraded and
    there is no second-best answer to return. The decision carries no share and a
    shortfall of the whole gang; the caller raises on it.
    """
    ranks = max(demand.gang_size, 1)
    request = structs.replace(
        _unit_request(demand),
        device_count=max(demand.unit_device_count, 1) * ranks,
        atomic=True,
    )
    decision = place(request, snapshot)
    if decision.placed and decision.cluster is not None:
        share = Share(cluster=decision.cluster, cluster_name=decision.cluster_name or decision.cluster, replicas=ranks)
        return ExpansionDecision(
            workload=demand.workload,
            shares=(share,),
            verdicts=decision.verdicts,
            desired=ranks,
            shortfall=0,
            splittable=False,
            summary=decision.summary,
        )
    return ExpansionDecision(
        workload=demand.workload,
        shares=(),
        verdicts=decision.verdicts,
        desired=ranks,
        shortfall=ranks,
        splittable=False,
        summary=_gang_summary(demand, ranks, decision.verdicts),
    )


def _unit_request(demand: WorkloadDemand) -> PlacementRequest:
    """One unit of the workload — the thing a share is counted in, placed whole."""
    return PlacementRequest(
        workload=demand.workload,
        project=demand.project,
        device_class=demand.device_class,
        device_count=max(demand.unit_device_count, 1),
        kind=demand.kind,
        priority_value=demand.priority_value,
        preempts=demand.preempts,
        device_memory_mib_min=demand.device_memory_mib_min,
        tensor_parallel=demand.tensor_parallel,
        requires_single_domain=demand.requires_single_domain,
        domain_kind=demand.domain_kind,
        deployment_name=demand.name,
        atomic=True,
    )


def _existing(demand: WorkloadDemand, by_uid: dict[str, ClusterVerdict]) -> list[ClusterVerdict]:
    return [by_uid[uid] for uid in sorted(demand.existing) if uid in by_uid]


def _capacity(demand: WorkloadDemand, verdicts: tuple[ClusterVerdict, ...]) -> dict[str, int]:
    """How many whole units each candidate cluster can take.

    A pin is an instruction rather than a hint: the named cluster is the only
    candidate and it is not checked for capacity, which is the whole point of the
    escape. Otherwise a candidate is a feasible verdict, and its ceiling is the
    lower of what is available — free, less what the cluster already owes deployments
    that have not bound yet, plus anything a preemption plan would reclaim — and what
    the project has left under quota in that cluster.
    """
    unit = max(demand.unit_device_count, 1)
    if demand.pinned_cluster is not None:
        return {v.cluster: UNBOUNDED for v in verdicts if v.cluster == demand.pinned_cluster}

    capacity: dict[str, int] = {}
    for verdict in verdicts:
        if not verdict.feasible:
            continue
        reclaimable = verdict.preemption.devices_reclaimed if verdict.preemption is not None else 0
        fits = (verdict.available_devices + reclaimable) // unit
        if verdict.quota_headroom is not None:
            fits = min(fits, verdict.quota_headroom // unit)
        if fits > 0:
            capacity[verdict.cluster] = fits
    return capacity


def _assign_floors(
    demand: WorkloadDemand,
    by_uid: dict[str, ClusterVerdict],
    capacity: dict[str, int],
    assigned: dict[str, int],
    desired: int,
) -> int:
    """Per-cluster minimums, honoured before anything is ranked.

    A floor that cannot be met is not an error — the shortfall stays in the remainder
    and shows up as the deployment's admission reason — and floors that add up to more
    than the total are honoured in cluster-name order until the total runs out, because
    silently scaling them down would answer a question the operator did not ask.
    """
    remaining = desired
    floors = [(uid, count) for uid, count in demand.cluster_floors.items() if uid in by_uid and count > 0]
    for uid, floor in sorted(floors, key=lambda item: by_uid[item[0]].cluster_name):
        if remaining <= 0:
            break
        take = min(floor, capacity.get(uid, 0), remaining)
        if take <= 0:
            continue
        if not _admits(demand, assigned, uid):
            continue
        assigned[uid] = assigned.get(uid, 0) + take
        remaining -= take
    return remaining


def _fill(
    demand: WorkloadDemand,
    by_uid: dict[str, ClusterVerdict],
    capacity: dict[str, int],
    assigned: dict[str, int],
    remaining: int,
) -> int:
    """The remainder, packed or spread, in whole units and in a fixed order."""
    if remaining <= 0:
        return max(remaining, 0)
    ordered = _fill_order(demand, by_uid, capacity)
    if demand.strategy == "spread":
        return _fill_spread(demand, capacity, assigned, ordered, remaining)
    return _fill_pack(demand, capacity, assigned, ordered, remaining)


def _fill_order(demand: WorkloadDemand, by_uid: dict[str, ClusterVerdict], capacity: dict[str, int]) -> list[str]:
    """Clusters already running this workload first, then the rest by the tiebreak.

    This is what makes re-expansion stable. A scale-up must not reshuffle a workload
    that is serving traffic, so a new cluster is reached for only once the ones
    already holding a share cannot absorb the increase.
    """
    ranked = sorted((by_uid[uid] for uid in capacity), key=_rank)
    running = [v.cluster for v in ranked if v.cluster in demand.existing]
    fresh = [v.cluster for v in ranked if v.cluster not in demand.existing]
    return running + fresh


def _fill_pack(
    demand: WorkloadDemand,
    capacity: dict[str, int],
    assigned: dict[str, int],
    ordered: list[str],
    remaining: int,
) -> int:
    for uid in ordered:
        if remaining <= 0:
            break
        if not _admits(demand, assigned, uid):
            continue
        take = min(remaining, capacity[uid] - assigned.get(uid, 0))
        if take <= 0:
            continue
        assigned[uid] = assigned.get(uid, 0) + take
        remaining -= take
    return remaining


def _fill_spread(
    demand: WorkloadDemand,
    capacity: dict[str, int],
    assigned: dict[str, int],
    ordered: list[str],
    remaining: int,
) -> int:
    """One unit at a time to whoever has the most room left, ties by the tiebreak.

    Deliberately not a proportional division: replicas are whole, and rounding a
    fraction produces a cluster holding a share nobody asked for. Handing them out
    one at a time gives the evenest integer split there is.
    """
    order = {uid: i for i, uid in enumerate(ordered)}
    while remaining > 0:
        eligible = [
            uid for uid in ordered if capacity[uid] - assigned.get(uid, 0) > 0 and _admits(demand, assigned, uid)
        ]
        if not eligible:
            break
        uid = min(eligible, key=lambda u: (-(capacity[u] - assigned.get(u, 0)), order[u]))
        assigned[uid] = assigned.get(uid, 0) + 1
        remaining -= 1
    return remaining


def _admits(demand: WorkloadDemand, assigned: dict[str, int], uid: str) -> bool:
    """The fan-out cap, applied to clusters that would newly hold a share.

    A cap is a limit on how many clusters this workload may straddle, so it bites
    when a share is about to open a new one; anything beyond it stays in the
    remainder and is reported as a shortfall rather than quietly dropped.
    """
    if uid in assigned:
        return True
    if demand.max_clusters is None:
        return True
    return len(assigned) < max(demand.max_clusters, 0)


def _expansion_summary(
    demand: WorkloadDemand, shares: tuple[Share, ...], shortfall: int, verdicts: tuple[ClusterVerdict, ...]
) -> str:
    if not shares:
        return _no_fit_summary(_unit_request(demand), verdicts)
    spread = ", ".join(f"{s.cluster_name}:{s.replicas}" for s in shares)
    placed = sum(s.replicas for s in shares)
    summary = f"{demand.workload} spread {placed} of {demand.desired_scale} over {len(shares)} cluster(s): {spread}"
    if shortfall > 0:
        summary += f"; {shortfall} unplaced"
    return summary


def _gang_summary(demand: WorkloadDemand, ranks: int, verdicts: tuple[ClusterVerdict, ...]) -> str:
    parts = [f"{v.cluster_name}: {v.reasons[0].message}" for v in verdicts if v.reasons]
    detail = "; ".join(parts) if parts else "no clusters in the snapshot"
    return f"gang of {ranks} does not fit in any single cluster; a gang cannot be split ({detail})"
