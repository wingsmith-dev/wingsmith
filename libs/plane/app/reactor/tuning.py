"""Per-object alerting overrides, read from labels that already exist.

The ask this answers is "let us adjust what alerts without shipping code", and the
tempting shape for it is a `monitoring_label` column on the workload. That column is
not here: `labels` is already a `JSONB` dict on `Workload`,
`Cluster`, `Pool`, `Node`, `Endpoint` and `DiscoveredWorkload`, already declared,
already editable through `PATCH` and already rendered in the console. A dedicated
column would be a second place to say the same thing, needing its own migration,
its own form field and its own CLI flag, and it would only ever work for the one
object it was added to.

So the reactor reads a reserved namespace out of the labels it already has:

    hangar.dev/alert            off | on
    hangar.dev/alert.severity   info | warning | error | critical
    hangar.dev/alert.for        45m | 2h | 900s | 900

The knob lives on the **workload**, and there is nothing above it to put it on: the
recipe that was once shared between workloads is gone, and muting on a shared object
would have quietened every workload built from that model at once: a much bigger
hammer than "this one is noisy". It is not on the deployment either: labels moved up
when `Workload` became the umbrella, and per-cluster muting would mean the same noisy
model had to be quietened once per region. `resolve` takes any number of label dicts
most-specific-first, so a project-level default is a one-line change if it turns out
to be wanted; nothing reads anything above the workload today.

Cluster conditions read `Cluster.labels` and stalled workloads read
`DiscoveredWorkload.labels`. The latter come from the cluster, not from us,
which makes the manifest the place to mute a workload nobody has imported: the
same knob, at the only altitude that exists before promotion.

Muting is `off` and nothing else. There is no "lower the severity until it stops
appearing" spelling, because that is what `severity` is for and conflating them
gives you an alert that is quiet in a way nobody can find later.
"""

from typing import Any
from dataclasses import dataclass

from .rules import SEVERITY_ORDER
from .rules import AlertIntent


PREFIX = "hangar.dev/alert"
SEVERITY_KEY = f"{PREFIX}.severity"
FOR_KEY = f"{PREFIX}.for"

OFF = {"off", "false", "no", "none", "0", "mute", "muted", "silence", "silenced"}

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(raw: str) -> int | None:
    """`45m`, `2h`, `900s`, or a bare number of seconds. None if it is not any of those.

    Returns None instead of raising on nonsense. A typo in a label must not stop
    the tick that would have told someone their cluster is down: the override is
    ignored, the rule's own window applies, and the alert still happens.
    """
    value = raw.strip().lower()
    if not value:
        return None
    unit = UNITS.get(value[-1])
    if unit is not None:
        value = value[:-1]
    else:
        unit = 1
    try:
        seconds = int(float(value) * unit)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


@dataclass(frozen=True)
class Override:
    """What one object's labels ask for. Every field optional: absent means "no opinion"."""

    muted: bool = False
    severity: str | None = None
    for_seconds: int | None = None

    @property
    def empty(self) -> bool:
        return not self.muted and self.severity is None and self.for_seconds is None


EMPTY = Override()


def read(labels: Any) -> Override:
    """Parses the reserved keys out of one label dict."""
    if not isinstance(labels, dict) or not labels:
        return EMPTY
    switch = str(labels.get(PREFIX, "")).strip().lower()
    severity = str(labels.get(SEVERITY_KEY, "")).strip().lower()
    window = labels.get(FOR_KEY)
    return Override(
        muted=switch in OFF and switch != "",
        severity=severity if severity in SEVERITY_ORDER else None,
        for_seconds=parse_duration(str(window)) if window is not None else None,
    )


def resolve(*label_sources: Any) -> Override:
    """Merges label dicts most-specific-first, field by field.

    Field by field, not whole-dict, so a deployment that only sets
    `hangar.dev/alert.for` still inherits the severity its workload chose. Whole-dict
    precedence would make every override an all-or-nothing copy of the parent's.
    """
    muted = False
    severity: str | None = None
    for_seconds: int | None = None
    for labels in label_sources:
        current = read(labels)
        muted = muted or current.muted
        if severity is None:
            severity = current.severity
        if for_seconds is None:
            for_seconds = current.for_seconds
    return Override(muted=muted, severity=severity, for_seconds=for_seconds)


def apply(intent: AlertIntent, *label_sources: Any) -> AlertIntent | None:
    """The tuned intent, or None if the labels muted it.

    Records what was applied in `details` under `tuned_by`, so an alert that fired
    at an unexpected severity, or a condition someone is wondering why they never
    hear about, can be traced back to the label that did it without anyone having
    to guess which object carried it.
    """
    override = resolve(*label_sources)
    if override.muted:
        return None
    if override.empty:
        return intent
    applied = {}
    if override.severity is not None:
        applied["severity"] = override.severity
    if override.for_seconds is not None:
        applied["for"] = override.for_seconds
    return AlertIntent(
        dedupe_key=intent.dedupe_key,
        rule=intent.rule,
        source=intent.source,
        severity=override.severity or intent.severity,
        subject_type=intent.subject_type,
        title=intent.title,
        subject_uid=intent.subject_uid,
        cluster_id=intent.cluster_id,
        cluster_name=intent.cluster_name,
        details={**intent.details, "tuned_by": applied},
        for_seconds=intent.for_seconds if override.for_seconds is None else override.for_seconds,
    )
