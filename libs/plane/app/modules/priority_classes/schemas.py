import datetime as dt

from app.core.schemas import UNSET
from app.core.schemas import Struct
from app.core.schemas import UnsetType


class PriorityClassSchema(Struct):
    """What preempts what.

    `value` is the whole ordering and it is total by construction: the service
    refuses a value another class already holds, because two classes at the same
    number make "which of these gets evicted first" a question the system answers
    differently on different days.

    `preemptible` and `preempts` are separate switches, not one axis. A class can
    take capacity from others and still be safe from eviction itself; folding
    them into a single flag would make production serving and best-effort batch
    the same kind of thing.
    """

    id: str
    name: str
    value: int
    preemptible: bool
    preempts: bool
    is_default: bool
    description: str | None
    created_at: dt.datetime
    updated_at: dt.datetime | None
    revision: str


class PriorityClassCreateRequest(Struct):
    name: str
    value: int
    preemptible: bool = True
    preempts: bool = False
    is_default: bool = False
    description: str | None = None


class PriorityClassUpdateRequest(Struct):
    """A partial update. An absent field is untouched; an explicit null clears."""

    value: int | UnsetType = UNSET
    preemptible: bool | UnsetType = UNSET
    preempts: bool | UnsetType = UNSET
    is_default: bool | UnsetType = UNSET
    description: str | None | UnsetType = UNSET
