import datetime as dt

from app.core.schemas import Struct


class AlertSubjectSchema(Struct):
    """What the alert is about, by public id.

    Unlike `EventSubjectSchema`, `id` is a resolvable `dep_`/`clu_`/`nod_`
    identifier — and it may name something that no longer exists. That is not a
    bug to fix later: an alert about a node that went away has to outlive the node,
    or the alerting system deletes its own evidence.
    """

    type: str
    id: str | None


class AlertSchema(Struct):
    """A derived condition, its lifecycle, and who has seen it.

    `state` and `severity` are two different axes and neither substitutes for the
    other. Severity is how bad it is; state is where it is in its life — `pending`
    (true, not yet for long enough to be worth telling anyone), `firing`,
    `acknowledged`, `resolved`. A pending alert is deliberately visible here and
    deliberately not notified anywhere.

    `for_seconds` is not returned, because it is a property of the rule rather than
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
