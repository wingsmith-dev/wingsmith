"""The single write path for declared objects: record every change, commit once (ADR 0037).

Every writer of a `Workload`, `Endpoint` or `Artifact` calls `record_change` for each object it
creates, modifies or deletes, before the transaction commits. Cascade deletes are not implicit:
the writer records a `DELETED` tombstone for every row the cascade removes.

`commit` is the unit-of-work boundary. Outside `unit_of_work` it commits and then runs the hooks
queued with `after_commit` (cluster wakeups); inside one it only flushes, and the outermost unit
commits, or rolls back for a dry run, so a manifest PUT composes several service calls into one
transaction.
"""

import datetime as dt
import contextlib

from uuid import UUID
from dataclasses import dataclass
from collections.abc import Callable
from collections.abc import Awaitable

from sqlalchemy import func
from sqlalchemy import text
from sqlalchemy import event
from sqlalchemy import delete
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy import inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm import object_session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Artifact
from app.db.models import Endpoint
from app.db.models import Workload
from app.db.models import ChangeLog
from app.db.models import ChangeLogState
from app.core.exceptions import Gone


ADDED = "ADDED"
MODIFIED = "MODIFIED"
DELETED = "DELETED"

WORKLOAD = "Workload"
ENDPOINT = "Endpoint"
ARTIFACT = "Artifact"

KINDS: dict[type, str] = {Workload: WORKLOAD, Endpoint: ENDPOINT, Artifact: ARTIFACT}
"""The classes the log covers, and the kind each is logged as."""

_CHANGES = "wingsmith_changes"
_HOOKS = "wingsmith_after_commit"
_DEPTH = "wingsmith_uow_depth"
_FLUSHED = "wingsmith_flushed"


@dataclass
class Change:
    kind: str
    id: UUID
    uid: str
    name: str
    project_id: UUID | None
    generation: int | None
    op: str
    obj: object
    forced: bool = False


def record_change(db: AsyncSession, obj: Workload | Endpoint | Artifact, op: str, force: bool = False) -> None:
    """Marks `obj` changed in this transaction. Call before the commit, after the row is flushed
    (it needs the id). A `MODIFIED` whose row the transaction never actually changed through the
    ORM logs nothing, so a replayed write keeps its resourceVersion; `force` logs it anyway, for a
    Core `UPDATE` the ORM did not see. Several records of one object collapse: `ADDED` stays `ADDED` unless it is
    deleted again, and `DELETED` always wins."""
    kind = KINDS.get(type(obj))
    if kind is None:
        return
    changes: dict[tuple[str, UUID], Change] = _info(db).setdefault(_CHANGES, {})
    key = (kind, obj.id)
    prev = changes.get(key)
    if prev is not None and op == MODIFIED:
        op = prev.op
        force = force or prev.forced
    project = obj.owner_project_id if isinstance(obj, Artifact) else obj.project_id
    generation = None if isinstance(obj, Artifact) else obj.generation
    changes[key] = Change(kind, obj.id, obj.uid, obj.name, project, generation, op, obj, force)


def _info(db) -> dict:
    target = getattr(db, "sync_session", None) or db
    info = getattr(target, "info", None)
    if not isinstance(info, dict):
        info = {}
        target.info = info
    return info


def pending(session) -> dict[tuple[str, UUID], Change]:
    """The changes recorded on a (sync) session and not yet committed."""
    return session.info.get(_CHANGES, {})


async def after_commit(db: AsyncSession, hook: Callable[[], Awaitable[None]]) -> None:
    """Runs `hook` now when no unit of work is open (the caller has already committed), else
    queues it for after the outermost commit. A rolled-back unit drops it."""
    if _info(db).get(_DEPTH, 0) == 0:
        await hook()
        return
    _info(db).setdefault(_HOOKS, []).append(hook)


async def commit(db: AsyncSession) -> None:
    """Commits, unless a `unit_of_work` is open, in which case it flushes and the unit commits."""
    if _info(db).get(_DEPTH, 0):
        await db.flush()
        return
    await db.commit()
    await _run_hooks(db)


async def _run_hooks(db: AsyncSession) -> None:
    hooks = _info(db).pop(_HOOKS, [])
    for hook in hooks:
        await hook()


@contextlib.asynccontextmanager
async def unit_of_work(db: AsyncSession):
    """One transaction for every `commit` inside the block. Yields a handle whose `discard()`
    makes the exit roll back instead (`dryRun=All`); an exception rolls back too."""
    info = _info(db)
    outer = info.get(_DEPTH, 0) == 0
    info[_DEPTH] = info.get(_DEPTH, 0) + 1
    unit = _Unit(db, outer)
    try:
        yield unit
    except BaseException:
        info[_DEPTH] -= 1
        if outer:
            info.pop(_HOOKS, None)
            await db.rollback()
        raise
    info[_DEPTH] -= 1
    if not outer or unit.committed:
        return
    if unit.discarded:
        info.pop(_HOOKS, None)
        await db.rollback()
        return
    await db.commit()
    await _run_hooks(db)


class _Unit:
    def __init__(self, db: AsyncSession, outer: bool) -> None:
        self.db = db
        self.outer = outer
        self.discarded = False
        self.committed = False

    def discard(self) -> None:
        """Roll back at the end of the block instead of committing."""
        self.discarded = True

    async def commit(self) -> None:
        """Commits now, before the block ends, so the caller can read what it committed. A no-op
        for a nested unit, whose outermost unit commits."""
        if not self.outer:
            await self.db.flush()
            return
        await self.db.commit()
        self.committed = True
        await _run_hooks(self.db)


def _note_written(mapper, _connection, target) -> None:
    """Which logged rows this transaction really wrote, so a `MODIFIED` that changed no column
    (a replay, or only a child collection) logs nothing. `after_update` fires for any dirty
    instance, so its columns' history is what decides."""
    session = object_session(target)
    if session is None:
        return
    state = inspect(target)
    if (
        state.deleted
        or not state.has_identity
        or any(state.attrs[c.key].history.has_changes() for c in mapper.column_attrs)
    ):
        session.info.setdefault(_FLUSHED, set()).add((KINDS[type(target)], target.id))


def _note_inserted(_mapper, _connection, target) -> None:
    session = object_session(target)
    if session is not None:
        session.info.setdefault(_FLUSHED, set()).add((KINDS[type(target)], target.id))


for _cls in KINDS:
    event.listen(_cls, "after_insert", _note_inserted)
    event.listen(_cls, "after_update", _note_written)
    event.listen(_cls, "after_delete", _note_inserted)


@event.listens_for(Session, "after_transaction_end")
def _forget(session: Session, transaction) -> None:
    """A committed or rolled-back top-level transaction takes its recorded changes with it."""
    if transaction.parent is None:
        session.info.pop(_CHANGES, None)
        session.info.pop(_FLUSHED, None)


LOCK_KEY = 0x77_73_63_6C  # "wscl": the one advisory lock every logging commit takes
"""Held from the log insert to the end of the commit, so seqs become visible in commit order: a
reader that has seen seq N will never later see a smaller one appear."""

CHANNEL = "wingsmith_changes"
"""`NOTIFY` on this channel after a logging commit, with the highest seq as payload. A wakeup only;
the log is what listeners read."""


@event.listens_for(Session, "before_commit")
def _write_log(session: Session) -> None:
    """Appends the recorded changes to `change_log` and stamps each live row's `resource_version`,
    as the last statements of the transaction."""
    if not session.info.get(_CHANGES):
        return
    session.flush()
    flushed = session.info.get(_FLUSHED, set())
    changes = [
        c for c in session.info.get(_CHANGES, {}).values() if c.op != MODIFIED or c.forced or (c.kind, c.id) in flushed
    ]
    if not changes:
        return
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
    seq = 0
    for change in changes:
        obj = change.obj
        generation = change.generation
        if change.op != DELETED and not isinstance(obj, Artifact):
            generation = obj.generation
        seq = session.execute(
            insert(ChangeLog)
            .values(
                kind=change.kind,
                project_id=change.project_id,
                name=change.name,
                uid=change.uid,
                op=change.op,
                generation=generation,
            )
            .returning(ChangeLog.seq)
        ).scalar_one()
        if change.op == DELETED:
            continue
        table = type(obj).__table__
        session.execute(update(table).where(table.c.id == change.id).values(resource_version=seq))
        if obj in session:
            set_committed_value(obj, "resource_version", seq)
    session.execute(text("SELECT pg_notify(:channel, :seq)"), {"channel": CHANNEL, "seq": str(seq)})


async def current(db: AsyncSession) -> int:
    """The newest committed seq, or how far the log was compacted when it is empty. A list read
    after this returns at least everything up to it, which is the RV the list reports."""
    newest = await db.scalar(select(func.max(ChangeLog.seq)))
    return newest if newest is not None else await compacted_through(db)


async def compacted_through(db: AsyncSession) -> int:
    return await db.scalar(select(ChangeLogState.compacted_through).where(ChangeLogState.id == 1)) or 0


async def since(db: AsyncSession, after: int, limit: int) -> tuple[list[ChangeLog], int]:
    """Changes with seq above `after`, oldest first, and how far the log is compacted. Raises
    `Gone` when `after` is at or below that point: the caller must relist."""
    through = await compacted_through(db)
    if through and after <= through:
        raise Gone(
            f"resourceVersion {after} is too old; the change log is compacted through {through}",
            {"compacted_through": through},
        )
    rows = await db.scalars(select(ChangeLog).where(ChangeLog.seq > after).order_by(ChangeLog.seq).limit(limit))
    return list(rows), through


async def compact(db: AsyncSession, older_than: dt.timedelta) -> int:
    """Deletes changes older than `older_than` and moves `compacted_through` past them. Returns
    the new `compacted_through`. Commits."""
    cutoff = dt.datetime.now(dt.UTC) - older_than
    newest_old = await db.scalar(select(func.max(ChangeLog.seq)).where(ChangeLog.created_at < cutoff))
    if newest_old is not None:
        await db.execute(
            pg_insert(ChangeLogState)
            .values(id=1, compacted_through=newest_old)
            .on_conflict_do_update(
                index_elements=[ChangeLogState.id],
                set_={"compacted_through": func.greatest(ChangeLogState.compacted_through, newest_old)},
            )
        )
        await db.execute(delete(ChangeLog).where(ChangeLog.seq <= newest_old))
    await db.commit()
    return await compacted_through(db)
