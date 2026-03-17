import datetime as dt

from app.core.schemas import Struct


class EventSubjectSchema(Struct):
    """What the event is about.

    `type` is the vocabulary the writer used; `id` is the subject's row
    identifier. It is not a prefixed public id, because `Event.subject_id` is an
    untyped UUID and resolving it to a `nod_...` would mean a join per possible
    subject type on the highest-volume table in the system.
    """

    type: str
    id: str | None


class EventSchema(Struct):
    """One observed, append-only state record.

    There is no declared half and no revision: nothing declares an event, and
    nothing may edit one, so there is nothing for `If-Match` to protect.
    """

    id: str
    cluster_id: str | None
    kind: str
    severity: str
    subject: EventSubjectSchema
    operation_id: str | None
    message: str
    details: dict
    occurred_at: dt.datetime
    recorded_at: dt.datetime
