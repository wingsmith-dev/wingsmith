"""What a hub cannot say about itself once it is gone: stale claims and stale reachability.

A hub session says when its cluster connects and when it leaves. A hub process that
dies without closing (killed, partitioned, out of memory) says neither, and two
rows go on asserting things that stopped being true: the claim naming a session
nobody is running, and `reachable` on a cluster nobody is talking to. Placement
reads the second.

Both carry a timestamp that a live session keeps moving on every ownership poll
(`heartbeat_at` on the claim, `last_seen_at` on the sync state), so staleness is a
fact the database can answer by itself. This pass runs on the reactor's lease
because it needs exactly one writer and the lease is the plane's one guarantee of
that.
"""

import datetime as dt

from logging import getLogger

from sqlalchemy import Delete
from sqlalchemy import Update
from sqlalchemy import or_
from sqlalchemy import func
from sqlalchemy import delete
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import HubClusterClaim
from app.db.models import ClusterSyncState


logger = getLogger(__name__)


STALE_POLLS = 6
"""How many ownership polls a live session may miss before its cluster reads as unheard from."""

MIN_STALE_SECONDS = 60.0


def stale_after() -> dt.timedelta:
    """The silence after which a claim is abandoned and a cluster is no longer reachable."""
    return dt.timedelta(seconds=max(settings.HUB_OWNERSHIP_CHECK_SECONDS * STALE_POLLS, MIN_STALE_SECONDS))


def expire_claims(window: dt.timedelta) -> Delete:
    """Removes claims whose session has not polled within `window`, judged on the database's clock.

    A live session whose claim is removed finds it gone on its next poll and closes as
    displaced, which is the right outcome for a session that could not poll for that long.
    """
    return delete(HubClusterClaim).where(HubClusterClaim.heartbeat_at < func.now() - window)


def unseen_clusters(moment: dt.datetime, window: dt.timedelta) -> Update:
    """Marks unreachable every cluster still claimed reachable that no session has touched within `window`.

    Judged on the application clock, because that is the clock `last_seen_at` is written
    with. A row with no stamp at all was never touched by a session that is still alive.
    """
    cutoff = moment - window
    return (
        update(ClusterSyncState)
        .where(
            ClusterSyncState.reachable.is_(True),
            or_(ClusterSyncState.last_seen_at.is_(None), ClusterSyncState.last_seen_at < cutoff),
        )
        .values(
            reachable=False,
            disconnected_at=moment,
            last_error=f"no hub has heard from this cluster's operator in {int(window.total_seconds())}s",
        )
    )


async def sweep(db: AsyncSession, moment: dt.datetime) -> tuple[int, int]:
    """Runs both statements and returns how many claims and clusters each one changed."""
    window = stale_after()
    claims = await db.execute(expire_claims(window))
    clusters = await db.execute(unseen_clusters(moment, window))
    if claims.rowcount or clusters.rowcount:
        logger.info("reactor.liveness.swept claims=%d clusters=%d", claims.rowcount, clusters.rowcount)
    return claims.rowcount, clusters.rowcount
