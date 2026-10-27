"""The plane's own repair pass: workloads short of their scale, and operations past their deadline.

Placement only ran when someone asked it to. A workload that landed short because
capacity was missing at the time stayed short after the capacity arrived; one whose
share was lost with a deleted or drained cluster was never placed again; and an
operation whose cluster never answered stayed `running` for ever. Nothing on a
timer looked at either.

This pass is that timer. It writes no intent of its own: a short workload is
re-expanded through `WorkloadService.rescale` with its own totals, exactly what
a person re-submitting the same scale would do, and only when placement says the
answer would now be larger. An operation past its `deadline_at` is closed as
`timed_out`, which is the phase the contract already reserves for it.

Run on the reactor's lease, so one process does it, and on a slower clock than
the alerting passes because re-expansion is the expensive thing here.
"""

import time
import datetime as dt

from uuid import UUID
from logging import getLogger

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Workload
from app.db.models import Operation
from app.db.models import Deployment
from app.modules.benchmarks.follow import follows_operation
from app.modules.workloads.schemas import WorkloadScaleParameters
from app.modules.placement.services import PlacementService
from app.modules.workloads.services import WorkloadService


logger = getLogger(__name__)


RECONCILE_SECONDS = 300.0

MANAGED = "managed"
"""Restated, like the operation phases below: the reactor reads module state, it does not import module constants."""

OPEN_OPERATION_PHASES = ("pending", "running")


class Cadence:
    """Whether enough time has passed since the last pass. In memory; a failover just runs one early."""

    __slots__ = ("_last", "interval")

    def __init__(self, interval: float = RECONCILE_SECONDS) -> None:
        self.interval = interval
        self._last = float("-inf")

    def due(self) -> bool:
        moment = time.monotonic()
        if moment - self._last < self.interval:
            return False
        self._last = moment
        return True


CADENCE = Cadence()


def short_workloads():
    """Workloads the plane manages that hold fewer replicas across their deployments than they ask for.

    Paused workloads are left alone, their shortfall is the point, and so is any
    workload with a deployment the plane did not create, because an adopted one's
    shape is the cluster's and re-expanding it would re-place something already
    running.
    """
    placed = (
        select(Deployment.workload_id, func.coalesce(func.sum(Deployment.desired_replicas), 0).label("placed"))
        .where(Deployment.authority == MANAGED)
        .group_by(Deployment.workload_id)
        .subquery()
    )
    adopted = select(Deployment.workload_id).where(Deployment.authority != MANAGED)
    return (
        select(Workload.id, Workload.uid, func.coalesce(placed.c.placed, 0).label("placed"))
        .outerjoin(placed, placed.c.workload_id == Workload.id)
        .where(
            Workload.paused.is_(False),
            Workload.desired_scale > func.coalesce(placed.c.placed, 0),
            Workload.id.notin_(adopted),
        )
        .order_by(Workload.uid)
    )


def expire_operations(moment: dt.datetime):
    """Closes every open operation whose deadline has passed, returning what it closed."""
    return (
        update(Operation)
        .where(
            Operation.phase.in_(OPEN_OPERATION_PHASES),
            Operation.deadline_at.is_not(None),
            Operation.deadline_at < moment,
        )
        .values(
            phase="timed_out",
            finished_at=moment,
            error="the operation passed its deadline without its cluster reporting an outcome",
        )
        .returning(Operation.uid)
    )


async def time_out_operations(db: AsyncSession, moment: dt.datetime) -> list[str]:
    """Times out overdue operations and carries the outcome onto any benchmark run they were carrying."""
    expired = list((await db.execute(expire_operations(moment))).scalars())
    for uid in expired:
        await db.execute(follows_operation(uid, "timed_out"))
    await db.commit()
    if expired:
        logger.info("reactor.reconcile.timed_out operations=%s", expired)
    return expired


async def _would_place_more(db: AsyncSession, workload_id: UUID, placed: int) -> bool:
    workload = await db.get(Workload, workload_id)
    if workload is None:
        return False
    decision = await PlacementService(db).expand(workload)
    return sum(share.replicas for share in decision.shares) > placed


async def replace_short_workloads(db: AsyncSession, limit: int = 50) -> list[str]:
    """Re-expands each short workload that placement could now give more to. Returns their uids.

    One workload at a time, each in its own commit, and a failure on one is logged
    and skipped: a gang that fits nowhere raises out of placement, and that is an
    answer about that workload, not a reason to stop looking at the rest.
    """
    rows = (await db.execute(short_workloads().limit(limit))).all()
    await db.rollback()
    replaced: list[str] = []
    for workload_id, uid, placed in rows:
        try:
            if not await _would_place_more(db, workload_id, placed):
                await db.rollback()
                continue
            await WorkloadService(db).rescale(uid, WorkloadScaleParameters())
            replaced.append(uid)
        except Exception as exc:  # noqa: BLE001 - one workload's refusal must not stop the sweep
            await db.rollback()
            logger.info("reactor.reconcile.skipped workload=%s reason=%s", uid, exc)
    if replaced:
        logger.info("reactor.reconcile.replaced workloads=%s", replaced)
    return replaced
