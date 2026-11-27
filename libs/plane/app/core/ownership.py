"""Who manages an applied object, and the guard that keeps other writers off it (ADR 0034).

A PUT records its field manager (`?fieldManager=`, `wings-cli` by default) on the object. From
then on a console PATCH, a workload operation, or a PUT from another manager is a 409 naming the
manager, unless it is forced: forcing takes the object over, which clears the manager and the
last-applied record and files a succeeded `<kind>.takeover` operation in the same transaction.
"""

import re
import datetime as dt

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm.util import identity_key
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import OPERATION
from app.core.ids import new_uid
from app.db.models import Operation
from app.db.changes import MODIFIED
from app.db.changes import record_change
from app.core.exceptions import Conflict
from app.core.exceptions import InvalidParameter


DEFAULT_MANAGER = "wings-cli"

_MANAGER = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,126}[A-Za-z0-9])?$")


def field_manager(value: str | None) -> str:
    """The `fieldManager` a request names, or the default. At most 128 characters, like Kubernetes."""
    if value is None or value == "":
        return DEFAULT_MANAGER
    if not _MANAGER.match(value):
        raise InvalidParameter(f"fieldManager {value!r} is not a valid manager name", {"field": "fieldManager"})
    return value


async def guard(
    db: AsyncSession,
    model,
    uid: str,
    *,
    kind: str,
    actor: str,
    force: bool = False,
    manager: str | None = None,
) -> None:
    """Refuses a write to an object another manager applied (409), or takes it over when forced.

    `manager` is the writer's own field manager, `None` for an imperative write (console, API).
    An object nobody manages, or one this manager already does, passes untouched.
    """
    # Lock the row so a write racing a first apply can't read a stale NULL manager and pass.
    stmt = select(model.id, model.name, model.applied_by).where(model.uid == uid).with_for_update()
    row = (await db.execute(stmt)).one()
    if row.applied_by is None or row.applied_by == manager:
        return
    if not force:
        raise Conflict(
            f"{kind} {row.name!r} is managed by {row.applied_by!r}; retry with force=true to take it over",
            {"manager": row.applied_by},
        )
    await db.execute(
        update(model)
        .where(model.uid == uid)
        .values(applied_by=None, applied_by_principal=None, applied_at=None, last_applied=None)
    )
    # Loaded only to name the change, then let go: a row loaded here would sit in the identity
    # map without the relationships the service's own joined read fills.
    loaded = identity_key(model, row.id) in db.sync_session.identity_map
    obj = await db.get(model, row.id)
    record_change(db, obj, MODIFIED, force=True)
    if not loaded:
        db.expunge(obj)
    now = dt.datetime.now(dt.UTC)
    db.add(
        Operation(
            uid=new_uid(OPERATION),
            type=f"{kind}.takeover",
            target_type=kind,
            target_id=row.id,
            phase="succeeded",
            requested_by=actor,
            reason=f"took over from {row.applied_by}",
            payload={"from": row.applied_by, "to": manager},
            started_at=now,
            finished_at=now,
        )
    )
    await db.flush()
