"""What counts as worth waking someone for. Pure, and the only place that decides.

Kept free of I/O on the same principle as `app/modules/placement`: the interesting part
of an alerting system is the judgement, and judgement that needs a database to
exercise does not get exercised. Everything here takes plain values and returns
`AlertIntent`, which `app.reactor.alerts` is then responsible for turning into a
row.

Two families, and the difference between them is not cosmetic:

- **Conditions** are level-triggered. `app.reactor.conditions` recomputes the
  entire set of currently-true conditions on every tick, so a key that stops
  appearing has stopped being true and its alert can be resolved automatically.
- **Events** are edge-triggered, matched against rows in `ops_event` as they
  arrive. An event is a thing that happened; the log will never retract it, so
  nothing derived from one may ever auto-resolve.

`for_seconds` is why an alert has a `pending` state. A condition that is true for
one tick is usually a reconcile in progress; one that is true for ten minutes is a
problem. Waiting is the difference between an alerting system people read and one
they filter into a folder.
"""

from typing import Any
from dataclasses import field
from dataclasses import dataclass


CONDITION = "condition"
EVENT = "event"

INFO = "info"
WARNING = "warning"
ERROR = "error"
CRITICAL = "critical"

SEVERITY_ORDER = {INFO: 0, WARNING: 1, ERROR: 2, CRITICAL: 3}


def at_least(severity: str, floor: str) -> bool:
    """Whether `severity` clears `floor`. Unknown severities are treated as the floor's equal.

    Deliberately permissive: an unrecognised severity arriving from a cluster is a
    contract drift worth seeing, and dropping it silently is the one response that
    guarantees nobody ever notices.
    """
    return SEVERITY_ORDER.get(severity, SEVERITY_ORDER[WARNING]) >= SEVERITY_ORDER.get(floor, SEVERITY_ORDER[WARNING])


@dataclass(frozen=True)
class AlertIntent:
    """One alert as the rules see it, before anything has been written down.

    `dedupe_key` is the identity and must be derivable from the condition alone —
    no timestamps, no counters, nothing that varies between two evaluations of the
    same ongoing problem. Getting this wrong does not produce an error; it
    produces a thousand alerts about one dead node.
    """

    dedupe_key: str
    rule: str
    source: str
    severity: str
    subject_type: str
    title: str
    subject_uid: str | None = None
    cluster_id: Any = None
    cluster_name: str | None = None
    details: dict = field(default_factory=dict)
    for_seconds: int = 0


CLUSTER_UNREACHABLE = "cluster.unreachable"
CLUSTER_NO_CAPACITY = "cluster.no_capacity"
DEPLOYMENT_BEHIND = "deployment.behind"
DEPLOYMENT_UNOBSERVED = "deployment.unobserved"
WORKLOAD_STALLED = "workload.stalled"

CONDITION_WINDOWS = {
    CLUSTER_UNREACHABLE: 300,
    CLUSTER_NO_CAPACITY: 900,
    DEPLOYMENT_BEHIND: 900,
    DEPLOYMENT_UNOBSERVED: 900,
    WORKLOAD_STALLED: 900,
}
"""How long each condition must hold before it is worth a notification.

Five minutes for reachability because a hub rollout is measured in seconds and a
real disconnect is not. Fifteen for everything to do with replicas, which is past
an image pull, a model load, and a node coming up, and well short of the seventeen
hours that prompted any of this.
"""


ALERTING_EVENTS = {
    "node.left": ERROR,
    "node.removed": ERROR,
    "node.degraded": WARNING,
    "device.fell_off_bus": ERROR,
    "device.degraded": WARNING,
    "replica.oom": ERROR,
    "replica.failed": ERROR,
    "replica.preempted": WARNING,
    "workload.removed": WARNING,
}
"""Event types that open an alert regardless of the severity the writer chose.

An override rather than a filter. Severity is set by whoever wrote the event —
the operator, for most of these — and `node.left` arriving as a `warning` is a
defensible call from inside a cluster that reschedules around it and the wrong
call for a single-node cluster whose only GPU just went away. Anything not named
here still alerts if it arrives at `error` or above, so this list is where a
judgement differs from the writer's, not a list of everything that matters.
"""


def event_intent(
    event_type: str,
    severity: str,
    subject_type: str,
    message: str,
    details: dict,
    event_uid: str,
    cluster_id: Any,
    cluster_name: str | None,
    floor: str = ERROR,
) -> AlertIntent | None:
    """An alert for one row of `ops_event`, or None if it is not worth one.

    The dedupe key includes the event's own uid, which looks wrong beside every
    other key here and is not. An event is a discrete occurrence: two OOMs an hour
    apart are two things that happened, and collapsing them into one alert with
    `occurrences: 2` would hide the second behind an alert somebody had already
    acknowledged. Conditions collapse; events accumulate.
    """
    override = ALERTING_EVENTS.get(event_type)
    if override is None and not at_least(severity, floor):
        return None
    resolved = override or severity
    return AlertIntent(
        dedupe_key=f"event:{event_uid}",
        rule=event_type,
        source=EVENT,
        severity=resolved,
        subject_type=subject_type,
        subject_uid=None,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        title=message or event_type,
        details={**details, "event_id": event_uid},
        for_seconds=0,
    )


def cluster_unreachable(cluster_id: Any, cluster_uid: str, cluster_name: str, last_seen: str | None) -> AlertIntent:
    seen = f"last seen {last_seen}" if last_seen else "never connected"
    return AlertIntent(
        dedupe_key=f"{CLUSTER_UNREACHABLE}:{cluster_uid}",
        rule=CLUSTER_UNREACHABLE,
        source=CONDITION,
        severity=ERROR,
        subject_type="cluster",
        subject_uid=cluster_uid,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        title=f"cluster {cluster_name} is not connected — {seen}",
        details={"last_seen_at": last_seen},
        for_seconds=CONDITION_WINDOWS[CLUSTER_UNREACHABLE],
    )


def cluster_no_capacity(cluster_id: Any, cluster_uid: str, cluster_name: str, nodes: int, devices: int) -> AlertIntent:
    """A cluster that is connected and reporting, and has nothing to run anything on.

    The condition the founding scenario needed and nothing had: a cluster whose
    only node was a spot instance is still perfectly reachable after that instance
    goes away, so every reachability check stays green while the cluster's actual
    capacity is zero.
    """
    return AlertIntent(
        dedupe_key=f"{CLUSTER_NO_CAPACITY}:{cluster_uid}",
        rule=CLUSTER_NO_CAPACITY,
        source=CONDITION,
        severity=ERROR,
        subject_type="cluster",
        subject_uid=cluster_uid,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        title=f"cluster {cluster_name} is connected but reports no devices ({nodes} nodes)",
        details={"nodes": nodes, "devices": devices},
        for_seconds=CONDITION_WINDOWS[CLUSTER_NO_CAPACITY],
    )


def deployment_behind(
    cluster_id: Any,
    cluster_name: str | None,
    deployment_uid: str,
    deployment_name: str,
    ready: int,
    desired: int,
) -> AlertIntent:
    return AlertIntent(
        dedupe_key=f"{DEPLOYMENT_BEHIND}:{deployment_uid}",
        rule=DEPLOYMENT_BEHIND,
        source=CONDITION,
        severity=ERROR if ready == 0 else WARNING,
        subject_type="deployment",
        subject_uid=deployment_uid,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        title=f"deployment {deployment_name} has {ready}/{desired} replicas ready",
        details={"ready_replicas": ready, "desired_replicas": desired},
        for_seconds=CONDITION_WINDOWS[DEPLOYMENT_BEHIND],
    )


def deployment_unobserved(
    cluster_id: Any, cluster_name: str | None, deployment_uid: str, deployment_name: str, desired: int
) -> AlertIntent:
    """Declared, enabled, and the cluster has never said a word about it.

    Distinct from `deployment.behind` because the remedy is different. Behind means
    the cluster is trying; unobserved means the plane's intent has not reached it at
    all, which is an operator, a sync, or a claim problem rather than a capacity one.
    """
    return AlertIntent(
        dedupe_key=f"{DEPLOYMENT_UNOBSERVED}:{deployment_uid}",
        rule=DEPLOYMENT_UNOBSERVED,
        source=CONDITION,
        severity=ERROR,
        subject_type="deployment",
        subject_uid=deployment_uid,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        title=f"deployment {deployment_name} has never been reported by its cluster",
        details={"desired_replicas": desired},
        for_seconds=CONDITION_WINDOWS[DEPLOYMENT_UNOBSERVED],
    )


def workload_stalled(
    cluster_id: Any,
    cluster_name: str | None,
    workload_uid: str,
    workload_name: str,
    ready: int,
    desired: int,
    promoted: bool,
) -> AlertIntent:
    """A workload the cluster is running — or failing to — that the plane has not adopted.

    This is the gap the founding scenario fell through. A sighting has no declared
    intent to compare against, so nothing that reasons about deployments can see it;
    but the sighting itself carries the cluster's own desired and ready counts, and
    zero ready against a non-zero desired is a broken workload whether or not anyone
    has got round to importing it.
    """
    note = "" if promoted else " (not imported into Hangar)"
    return AlertIntent(
        dedupe_key=f"{WORKLOAD_STALLED}:{workload_uid}",
        rule=WORKLOAD_STALLED,
        source=CONDITION,
        severity=ERROR,
        subject_type="discovered_workload",
        subject_uid=workload_uid,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        title=f"workload {workload_name} has {ready}/{desired} replicas ready{note}",
        details={"replicas_ready": ready, "replicas_desired": desired, "promoted": promoted},
        for_seconds=CONDITION_WINDOWS[WORKLOAD_STALLED],
    )
