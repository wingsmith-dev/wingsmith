"""Advancing a cluster's declared phase when its operator first enrols.

Registration writes a cluster in `pending`. The first time its operator dials
the hub and completes the handshake, intent should move past bootstrap to
`active` — a declared write, made here by the clusters module, which owns the
declared half. It is deliberately *not* done by ingestion: observed state
(`reachable`, `sync`) has exactly one writer, and `phase` is not it (see
`DatabaseSink.mark_connected`, which will not touch phase). The hub session
calls this after a successful `welcome`.
"""

import asyncio
import datetime as dt

from logging import getLogger

from sqlalchemy import select

from app.db.engine import session_factory
from app.db.models import Cluster


logger = getLogger(__name__)

_TASKS: set[asyncio.Task] = set()
"""Live promotion tasks, held so the event loop does not garbage-collect one
mid-flight. Entries remove themselves on completion."""


def _log_if_failed(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("cluster.enrolment.promote_failed error=%s", exc)


def schedule_promote_on_enrolment(cluster_uid: str) -> None:
    """Promote `cluster_uid` detached from the caller's coroutine.

    The promotion touches the database. Awaiting it inline on the hub's
    handshake path would put a round trip between `welcome` and the first
    streamed frame — and enrolment intent is not something the operator's stream
    should wait on. Scheduling a task does not yield the loop, so it cannot
    reorder frames the caller goes on to send; failures are logged, never raised
    back into the handshake.
    """
    task = asyncio.ensure_future(promote_on_enrolment(cluster_uid))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    task.add_done_callback(_log_if_failed)


async def promote_on_enrolment(cluster_uid: str) -> None:
    """Flip `pending → active` and stamp `enrolled_at`, once, for `cluster_uid`.

    Idempotent and cheap: a no-op unless the cluster is still `pending`, so it
    can run on every handshake — including a resume — without fighting an
    operator who has since moved the cluster to `draining` or `disabled`. Runs
    in its own transaction because the caller (the hub session) holds no request
    database session, and swallows nothing — a failure here must not sink the
    handshake, so the caller logs and carries on.
    """
    async with session_factory()() as db:
        cluster = (await db.execute(select(Cluster).where(Cluster.uid == cluster_uid))).scalar_one_or_none()
        if cluster is None or cluster.phase != "pending":
            return
        cluster.phase = "active"
        if cluster.enrolled_at is None:
            cluster.enrolled_at = dt.datetime.now(dt.UTC)
        await db.commit()
        logger.info("cluster.enrolment.promoted cluster=%s phase=active", cluster_uid)
