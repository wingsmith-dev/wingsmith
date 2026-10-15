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

A condition may also come from a number an operator declared rather than from a
constant here: `METRIC_RULES` names the metric and the direction, `ops_alert_threshold`
carries the value, the window and the scope, and `app.reactor.conditions` reads both and
hands this module plain floats. The measurement is fetched by the caller precisely so
that the judgement stays exercisable without a metrics backend.

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
an image pull, a model load, and a node coming up.
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


ABOVE = "above"
BELOW = "below"
COMPARISONS = (ABOVE, BELOW)


@dataclass(frozen=True)
class MetricRule:
    """One metric-backed check: which measurement, which way, and what it falls back to.

    The predicate is code and the number is data. A rule names a metric by its
    `app.modules.metrics.registry` name and never by a series or an expression —
    an upstream rename is then a change to one dialect table and not to anything
    an operator has declared.

    `comparison` lives here rather than on the row because which way a metric is
    wrong is judgement, not a commitment: nobody declares that a cache hit rate
    being *high* is the problem. What is declared is the number, the window and
    the scope, and those are the columns `ops_alert_threshold` has.

    `suggested` is what the number would be if nobody had said otherwise. It is
    not a default that fires: no metric rule evaluates until an operator declares
    a threshold row for it, which is also what keeps the fan-out of the metric
    pass proportional to the commitments rather than to the fleet.
    """

    rule: str
    metric: str
    comparison: str
    subject_type: str
    severity: str
    noun: str
    suggested: float
    lookback: str = "5m"
    for_seconds: int = 900


METRIC_RULES: dict[str, MetricRule] = {
    rule.rule: rule
    for rule in (
        MetricRule(
            rule="deployment.ttft_high",
            metric="inference.ttft",
            comparison=ABOVE,
            subject_type="deployment",
            severity=WARNING,
            noun="time to first token (p95)",
            suggested=2.0,
        ),
        MetricRule(
            rule="deployment.ttft_p90_high",
            metric="inference.ttft_p90",
            comparison=ABOVE,
            subject_type="deployment",
            severity=WARNING,
            noun="time to first token (p90)",
            suggested=1.5,
        ),
        MetricRule(
            rule="deployment.latency_high",
            metric="inference.latency",
            comparison=ABOVE,
            subject_type="deployment",
            severity=ERROR,
            noun="end-to-end latency (p95)",
            suggested=30.0,
        ),
        MetricRule(
            rule="deployment.queue_time_high",
            metric="inference.queue_time",
            comparison=ABOVE,
            subject_type="deployment",
            severity=WARNING,
            noun="time waiting in queue (p95)",
            suggested=1.0,
        ),
        MetricRule(
            rule="deployment.output_speed_below_floor",
            metric="inference.output_tokens_per_stream",
            comparison=BELOW,
            subject_type="deployment",
            severity=WARNING,
            noun="output speed for one stream",
            suggested=30.0,
        ),
        MetricRule(
            rule="deployment.prefix_cache_hit_rate_low",
            metric="inference.prefix_cache_hit_rate",
            comparison=BELOW,
            subject_type="deployment",
            severity=INFO,
            noun="prefix cache hit rate",
            suggested=20.0,
            lookback="30m",
            for_seconds=3600,
        ),
        MetricRule(
            rule="deployment.kv_cache_saturated",
            metric="inference.kv_cache_utilization",
            comparison=ABOVE,
            subject_type="deployment",
            severity=WARNING,
            noun="KV cache utilization",
            suggested=95.0,
        ),
        MetricRule(
            rule="deployment.preemptions_high",
            metric="inference.preemptions",
            comparison=ABOVE,
            subject_type="deployment",
            severity=WARNING,
            noun="preemptions per second",
            suggested=0.5,
        ),
        MetricRule(
            rule="node.gpu_temperature_high",
            metric="gpu.temperature",
            comparison=ABOVE,
            subject_type="node",
            severity=WARNING,
            noun="hottest GPU",
            suggested=87.0,
            for_seconds=600,
        ),
        MetricRule(
            rule="node.ecc_uncorrectable",
            metric="gpu.ecc_uncorrectable",
            comparison=ABOVE,
            subject_type="node",
            severity=ERROR,
            noun="uncorrectable ECC errors",
            suggested=0.0,
            for_seconds=0,
        ),
    )
}
"""Every metric-backed rule, by name. The registry of predicates a threshold row may name.

Eight of these read the serving engine and two read the cards. **None of them reads
an availability, failure-rate or error-ratio series, and that is a decision rather
than an omission.** A request that never reached the cluster — refused at the front
door, routed nowhere, timed out upstream — emits nothing an engine exporter can
count, so a ratio built out of these series reports a fully-broken cluster as "no
traffic" rather than as an outage, and the alert that matters most is the one
guaranteed not to fire. Availability has to be measured at the hop that saw the
request, which is not a cluster-side exporter and is not in
`app.modules.metrics.registry`. A rule here that claimed otherwise would be worse
than the absence of one: it would be a green light with nothing behind it.

`METRIC_SUBJECTS` is derived from this table rather than declared beside it, so a
rule for a new kind of subject cannot be added without the evaluator noticing.
"""

METRIC_SUBJECTS: tuple[str, ...] = tuple(sorted({rule.subject_type for rule in METRIC_RULES.values()}))


@dataclass(frozen=True)
class Threshold:
    """One `ops_alert_threshold` row, flattened to plain values before it gets here.

    `value` is `Numeric` in the database and therefore `Decimal` at runtime; the
    caller converts. Rules compare and format plain floats, which is what keeps
    them exercisable from a test that has never opened a session.

    `subject_uid` of None is the fleet default for its rule — one row that covers
    every subject of that type — and a row naming a subject overrides it for that
    subject alone.
    """

    rule: str
    subject_type: str
    value: float
    subject_uid: str | None = None
    dimensions: dict = field(default_factory=dict)
    objective: str | None = None
    lookback: str | None = None
    for_seconds: int | None = None
    severity: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class MetricSubject:
    """The object one metric rule is being evaluated for, named the way an alert names it."""

    uid: str
    name: str
    cluster_id: Any = None
    cluster_name: str | None = None


def _dimension_key(dimensions: dict) -> str:
    """A stable suffix for the dedupe key, so two narrowings of one rule stay two alerts."""
    if not dimensions:
        return ""
    return ":" + ",".join(f"{key}={dimensions[key]}" for key in sorted(dimensions))


def _specificity(threshold: Threshold) -> tuple:
    """Most specific first: a row about this subject, then the fewest narrowing dimensions.

    Ordered rather than filtered, and total rather than partial, because the
    alternative is letting the planner's row order decide which commitment applies
    — the same failure `uq_ops_alert_threshold_scope` uses `NULLS NOT DISTINCT` to
    keep out of the table in the first place.
    """
    return (
        0 if threshold.subject_uid is not None else 1,
        len(threshold.dimensions),
        _dimension_key(threshold.dimensions),
    )


def resolve_threshold(thresholds: list[Threshold], rule: str, subject_uid: str) -> Threshold | None:
    """The threshold that applies to one subject for one rule, or None if nobody declared one.

    A scoped row beats the fleet default; among rows at the same scope the least
    narrowed one wins, because a dimensioned row asks a question this evaluator
    cannot put to the store yet and an undimensioned sibling can be answered
    honestly today.
    """
    candidates = [t for t in thresholds if t.rule == rule and t.subject_uid in (None, subject_uid)]
    if not candidates:
        return None
    return min(candidates, key=_specificity)


def breached(comparison: str, observed: float, value: float) -> bool:
    """Whether a measurement is on the wrong side of a declared number.

    An unrecognised comparison is not a breach. The catalogue is code, so this is
    only reachable mid-edit, and a rule that fires on a spelling mistake is a rule
    nobody trusts afterwards.
    """
    if comparison == ABOVE:
        return observed > value
    if comparison == BELOW:
        return observed < value
    return False


def _number(value: float) -> str:
    return f"{value:g}"


def metric_breach(subject: MetricSubject, threshold: Threshold, observed: float | None) -> AlertIntent | None:
    """The alert one declared threshold produces for one subject, or None.

    **`observed is None` never alerts.** None is "the store had no series", which
    is not zero and is not a breach: an engine that has served nothing reports
    nothing, and a rule that read that silence as a floor being missed would page
    someone every night for every idle deployment. It is the same asymmetry that
    keeps an availability rule out of `METRIC_RULES` — what a cluster-side
    exporter does not say is not evidence of anything.

    Everything the alert was decided by goes into `details`: the metric's registry
    name, the comparison, both numbers, the window, and the `reason` the operator
    wrote on the row. An alert whose threshold cannot be read next to it is one
    somebody has to go and look up the commitment for, at the hour when that is
    hardest.
    """
    definition = METRIC_RULES.get(threshold.rule)
    if definition is None or observed is None:
        return None
    if not breached(definition.comparison, observed, threshold.value):
        return None
    window = threshold.lookback or definition.lookback
    return AlertIntent(
        dedupe_key=f"{threshold.rule}:{subject.uid}{_dimension_key(threshold.dimensions)}",
        rule=threshold.rule,
        source=CONDITION,
        severity=threshold.severity or definition.severity,
        subject_type=definition.subject_type,
        subject_uid=subject.uid,
        cluster_id=subject.cluster_id,
        cluster_name=subject.cluster_name,
        title=(
            f"{subject.name}: {definition.noun} is {_number(observed)} {definition.comparison}"
            f" the declared {_number(threshold.value)} over {window}"
        ),
        details={
            "metric": definition.metric,
            "comparison": definition.comparison,
            "observed": observed,
            "threshold": threshold.value,
            "lookback": window,
            "dimensions": threshold.dimensions,
            "objective": threshold.objective,
            "declared_reason": threshold.reason,
        },
        for_seconds=definition.for_seconds if threshold.for_seconds is None else threshold.for_seconds,
    )
