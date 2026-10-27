import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class AlertSubjectSchema(Struct):
    """What the alert is about, by public id.

    Unlike `EventSubjectSchema`, `id` is a resolvable `dep_`/`clu_`/`nod_`
    identifier, and it may name something that no longer exists. That is not a
    bug to fix later: an alert about a node that went away has to outlive the node,
    or the alerting system deletes its own evidence.
    """

    type: str
    id: str | None


class AlertSchema(Struct):
    """A derived condition, its lifecycle, and who has seen it.

    `state` and `severity` are two different axes and neither substitutes for the
    other. Severity is how bad it is; state is where it is in its life: `pending`
    (true, not yet for long enough to be worth telling anyone), `firing`,
    `acknowledged`, `resolved`. A pending alert is intentionally visible here and
    intentionally not notified anywhere.

    `for_seconds` is not returned, because it is a property of the rule, not
    of this occurrence; `first_seen_at` and `fired_at` together say everything a
    caller needs about how long it took.
    """

    id: str
    rule: str
    source: str
    severity: str
    state: str
    title: str
    cluster_id: str | None
    subject: AlertSubjectSchema
    details: dict
    occurrences: int
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    fired_at: dt.datetime | None
    resolved_at: dt.datetime | None
    acknowledged_at: dt.datetime | None
    acknowledged_by: str | None
    acknowledged_note: str | None
    notified_at: dt.datetime | None
    notify_error: str | None


class AlertAcknowledgeRequest(Struct):
    """`note` is optional and is the only thing a human adds that the reactor cannot.

    There is no `by` field: the acknowledger is the caller, taken from the request
    principal. Accepting it from the body would make an audit trail that anyone can
    write anyone else's name into.
    """

    note: str | None = None


class MetricRuleSchema(Struct):
    """One entry in the catalogue of rules a threshold may name.

    Read-only, and code, not data: the metric and the comparison are the
    judgement `app/reactor/rules.py` owns, and the only reason they are on the wire
    at all is that a client building a threshold form has to know which number it
    is being asked for and in which unit. `metric` is a name from the metrics
    registry, never an expression: an operator who could write the query could
    write one that means something else entirely.
    """

    name: str
    metric: str
    unit: str
    comparison: str
    subject_type: str
    severity: str
    measures: str
    suggested_value: float
    lookback: str
    for_seconds: int


class AlertThresholdSchema(Struct):
    """A number this plane has committed to, and the rule that watches it.

    `subject.id` of null is the fleet default for that rule (one row that covers
    every subject of its type), and a row naming a subject overrides it for that
    subject alone.

    `lookback`, `for_seconds` and `severity` are returned exactly as declared,
    null included, because null here means "whatever the rule says" and
    substituting the rule's own value would make a row look like it had pinned
    something it intentionally left alone. `/v1/alert-rules` carries the defaults.
    """

    id: str
    rule: str
    metric: str
    comparison: str
    subject: AlertSubjectSchema
    dimensions: dict
    value: float
    objective: str | None
    lookback: str | None
    for_seconds: int | None
    severity: str | None
    enabled: bool
    reason: str | None
    created_at: dt.datetime
    updated_at: dt.datetime | None


class AlertThresholdCreateRequest(Struct):
    """`comparison` is intentionally absent. Which way a metric is wrong is not declarable."""

    rule: str
    value: float
    subject_type: str | None = None
    subject_id: str | None = None
    dimensions: dict = {}
    objective: str | None = None
    lookback: str | None = None
    for_seconds: int | None = None
    severity: str | None = None
    enabled: bool = True
    reason: str | None = None


class AlertThresholdUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears.

    `rule`, `subject_type` and `subject_id` are not updatable. They are the row's
    identity under `uq_ops_alert_threshold_scope`, and moving a commitment onto a
    different rule or a different subject is the creation of a different
    commitment: one that should leave the old row's history behind it.
    """

    value: float | UnsetType = UNSET
    dimensions: dict | UnsetType = UNSET
    objective: str | None | UnsetType = UNSET
    lookback: str | None | UnsetType = UNSET
    for_seconds: int | None | UnsetType = UNSET
    severity: str | None | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
    reason: str | None | UnsetType = UNSET
