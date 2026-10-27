"""Events the hub itself raises, kept out of `dispatch` so that module stays about sending.

The hub is the one process that knows whether a cluster was actually told
something. The API knows what was asked for and ingestion knows what the cluster
concluded; between the two there is a gap, desired state written, cluster
silent, and until something wrote this down, that gap was indistinguishable
from a hub that never delivered at all.
"""

import datetime as dt

from logging import getLogger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Cluster
from app.db.models import Deployment
from app.db.notify import DeclaredChange
from app.ingestion import statements


logger = getLogger(__name__)


async def record_dispatch(db: AsyncSession, change: DeclaredChange, frames: int) -> None:
    """Files that a deployment's declared state went down a cluster's session.

    Resolved against the database, not trusted from the hint, for the same
    reason the renderer re-reads the row it is about to send: the hint names an
    object and asserts nothing about it. A hint for a deployment that has since
    been deleted renders nothing, so this is never reached for one, but a hint
    whose cluster reference is a name instead of a uid is routine, and that is
    what `cluster_ref_clause` is for.
    """
    row = (
        await db.execute(
            select(Deployment.id, Deployment.name, Deployment.cluster_id)
            .join(Cluster, Cluster.id == Deployment.cluster_id)
            .where(Deployment.uid == change.uid, statements.cluster_ref_clause(change.cluster_id))
        )
    ).first()
    if row is None:
        logger.debug("hub.record.unknown_deployment uid=%s cluster=%s", change.uid, change.cluster_id)
        return

    await db.execute(
        statements.deployment_lifecycle(
            row.cluster_id,
            row.id,
            "dispatched",
            f"{row.name} was sent to {change.cluster_id}",
            {
                "deployment_id": change.uid,
                "name": row.name,
                "generation": str(change.generation),
                "frames": str(frames),
            },
            dt.datetime.now(dt.UTC),
        )
    )
    await db.commit()
