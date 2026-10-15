"""The `revision` every declared resource carries, and the `If-Match` check against it.

A revision is the row's last write in microseconds, derived from `updated_at` rather than stored,
so it cannot disagree with the row.
"""

import datetime as dt

from typing import Protocol

from app.core.exceptions import Conflict


class Stamped(Protocol):
    created_at: dt.datetime
    updated_at: dt.datetime | None


def stamp_revision(stamp: dt.datetime) -> str:
    return str(int(stamp.timestamp() * 1_000_000))


def revision(row: Stamped) -> str:
    return stamp_revision(row.updated_at or row.created_at)


def check_if_match(row: Stamped, if_match: str | None, what: str) -> None:
    """Raises `Conflict` carrying the current revision when `If-Match` names an older one."""
    if if_match is not None and if_match != revision(row):
        raise Conflict(f"{what} changed since it was read", {"revision": revision(row)})
