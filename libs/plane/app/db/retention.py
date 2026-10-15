"""Deletes old rows from `ops_event`, the one plane table that only grows.

Run it on a schedule, for example a daily Kubernetes CronJob on the plane image:

    prune-events --older-than-days 90

or `uv run python -m app.db.retention --older-than-days 90` from a checkout.

An event is deleted only when it is older than the cutoff and the reactor's cursor
(`ops_reactor_state.last_event_seq`) has passed it, so a reactor that has been down
still matches what it missed. Deletes run in batches, one transaction each, so a large
backlog never holds one long lock on the table.
"""

import asyncio
import argparse
import datetime as dt

from typing import cast
from logging import getLogger

from sqlalchemy import Delete
from sqlalchemy import CursorResult
from sqlalchemy import delete
from sqlalchemy import exists
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.utils import logs
from app.db.engine import dispose
from app.db.engine import session_factory
from app.db.models import Event
from app.db.models import ReactorState


logger = getLogger(__name__)

DEFAULT_DAYS = 90
DEFAULT_BATCH = 5000


def prune_statement(before: dt.datetime, batch: int) -> Delete:
    """One batch: the oldest events before `before` that every reactor cursor has passed."""
    unread = exists().where(ReactorState.last_event_seq < Event.seq)
    oldest = (
        select(Event.id)
        .where(Event.occurred_at < before)
        .where(~unread)
        .order_by(Event.seq)
        .limit(batch)
        .scalar_subquery()
    )
    return delete(Event).where(Event.id.in_(oldest))


async def prune_events(
    sessions: async_sessionmaker[AsyncSession], before: dt.datetime, batch: int = DEFAULT_BATCH
) -> int:
    """Deletes prunable events in batches until none are left, returning how many went."""
    if batch < 1:
        raise ValueError("batch must be at least 1")
    total = 0
    while True:
        async with sessions.begin() as db:
            result = cast(CursorResult, await db.execute(prune_statement(before, batch)))
            deleted = result.rowcount
        total += deleted
        if deleted < batch:
            return total


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="prune-events", description=__doc__.splitlines()[0])
    parser.add_argument("--older-than-days", type=int, default=DEFAULT_DAYS, help="default: %(default)s")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="rows per transaction, default: %(default)s")
    args = parser.parse_args(argv)
    if args.older_than_days < 1:
        parser.error("--older-than-days must be at least 1")
    if args.batch < 1:
        parser.error("--batch must be at least 1")
    return args


async def run(args: argparse.Namespace) -> int:
    before = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.older_than_days)
    try:
        deleted = await prune_events(session_factory(), before, args.batch)
    finally:
        await dispose()
    logger.info("pruned %d events that occurred before %s", deleted, before.isoformat())
    return deleted


def main(argv: list[str] | None = None) -> None:
    args = parse(argv)
    logs.configure("retention")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
