"""Durable ownership of a cluster's stream (ADR 0005), the correctness fix for a hub
that no longer runs one replica.

`SessionState` stays in memory and is not shared across pods. What does have to be
shared is a single fact: exactly one session may be writing for a given `cluster_id`
at any moment, or two pods can interleave writes to the same ingestion rows. This
module is that fact's only home — a row per `cluster_id` in `hub_cluster_claim`,
claimed atomically at `hello` and polled by the session that currently believes it
holds it.

Claiming is unconditional by design: a `hello` is, by construction, the newest claim
attempt for its cluster, so there is nothing to compare against before overwriting.
The atomicity that matters is not "newer beats older" arithmetic, it is that the
read-and-decide never happens as two statements — a single `INSERT ... ON CONFLICT
DO UPDATE` is the whole claim, so two pods racing to claim the same cluster cannot
both observe themselves as the winner.

A displaced session is never pushed a notification — it may be mid-stream on a pod
that the newer claimant has no channel to. Instead every live session polls its own
row on an interval (`HUB_OWNERSHIP_CHECK_SECONDS`) and closes itself the moment the
row names a different `session_id`. The peer reconnects, most likely to a different
pod under a Kubernetes Service, and the hub there resyncs it from scratch — a
`full_resync`, per PROTOCOL §4, is the correct and only consequence.
"""

from typing import Protocol
from logging import getLogger

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import session_factory
from app.db.models import HubClusterClaim


logger = getLogger(__name__)


class ClusterClaimRepository:
    """Machinery: the service owns the commit, this only flushes."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def upsert(self, cluster_id: str, session_id: str, instance: str) -> HubClusterClaim:
        """Atomically claims `cluster_id`. One statement: no read-then-write to race."""
        stmt = insert(HubClusterClaim).values(
            cluster_id=cluster_id,
            session_id=session_id,
            instance=instance,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[HubClusterClaim.cluster_id],
            set_={
                "session_id": stmt.excluded.session_id,
                "instance": stmt.excluded.instance,
                "claimed_at": stmt.excluded.claimed_at,
                "heartbeat_at": stmt.excluded.heartbeat_at,
            },
        ).returning(HubClusterClaim)
        result = await self.db.execute(stmt)
        row = result.scalar_one()
        await self.db.flush()
        return row

    async def get(self, cluster_id: str) -> HubClusterClaim | None:
        result = await self.db.execute(select(HubClusterClaim).where(HubClusterClaim.cluster_id == cluster_id))
        return result.scalar_one_or_none()

    async def touch(self, cluster_id: str, session_id: str) -> bool:
        result = await self.db.execute(
            select(HubClusterClaim).where(
                HubClusterClaim.cluster_id == cluster_id,
                HubClusterClaim.session_id == session_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await self.db.flush()
        return True


class ClusterClaimService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ClusterClaimRepository(db)

    async def claim(self, cluster_id: str, session_id: str, instance: str) -> None:
        """Claims `cluster_id` for `session_id`, unconditionally displacing any prior owner."""
        await self.repo.upsert(cluster_id, session_id, instance)
        await self.db.commit()

    async def owns(self, cluster_id: str, session_id: str) -> bool:
        """Whether `session_id` is still the row's current owner."""
        row = await self.repo.get(cluster_id)
        await self.db.commit()
        return row is not None and row.session_id == session_id


class ClaimStore(Protocol):
    """The seam `OperatorSession` claims and checks ownership through.

    Narrow on purpose, matching `app.hub.sink.Sink`: a session must be exercisable
    end to end with no database, so tests use `MemoryClaimStore` in its place.
    """

    async def claim(self, cluster_id: str, session_id: str, instance: str) -> None: ...

    async def owns(self, cluster_id: str, session_id: str) -> bool: ...


class DatabaseClaimStore:
    """Session-per-call, matching `DatabaseSink` — the hub has no request scope to
    borrow a session from, and this path is hit once per `hello` and then once per
    ownership poll, not once per frame.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sessions = sessions

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._sessions is None:
            self._sessions = session_factory()
        return self._sessions

    async def claim(self, cluster_id: str, session_id: str, instance: str) -> None:
        async with self.sessions() as db:
            await ClusterClaimService(db).claim(cluster_id, session_id, instance)

    async def owns(self, cluster_id: str, session_id: str) -> bool:
        async with self.sessions() as db:
            return await ClusterClaimService(db).owns(cluster_id, session_id)


class MemoryClaimStore:
    """In-process stand-in for tests and for the single-replica default.

    Still enforces single ownership within the process, which is exactly what the
    pre-ADR-0005 code lacked (`app.hub.session._resolve_session` docstring).
    """

    def __init__(self) -> None:
        self._owners: dict[str, tuple[str, str]] = {}

    async def claim(self, cluster_id: str, session_id: str, instance: str) -> None:
        self._owners[cluster_id] = (session_id, instance)

    async def owns(self, cluster_id: str, session_id: str) -> bool:
        owner = self._owners.get(cluster_id)
        return owner is not None and owner[0] == session_id
