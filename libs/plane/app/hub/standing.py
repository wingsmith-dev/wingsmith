"""Whether a cluster may hold a hub session right now.

The enrolment token is checked once, when the socket opens. Everything after
that used to run on the strength of that one check: a cluster disabled in the
registry, or one whose token was rotated because it leaked, kept its session,
and kept writing observed state, until the operator happened to reconnect.

So the question is asked twice. At `hello`, before anything is claimed or
streamed, and then on every ownership poll for as long as the session lives.
A refusal at either point ends the session; the operator's reconnect then meets
the same answer at the door.
"""

from typing import Protocol
from logging import getLogger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.engine import session_factory
from app.db.models import Cluster
from app.ingestion.statements import cluster_ref_clause


logger = getLogger(__name__)


UNKNOWN = "cluster is not registered"
DISABLED = "cluster is disabled"
ROTATED = "the enrolment token this session opened with was rotated"


class Standing(Protocol):
    """The seam `OperatorSession` asks through. `None` admits; a string is the refusal."""

    async def refusal(self, cluster_uid: str, credential: str | None) -> str | None: ...


def judge(phase: str | None, token_hash: str | None, credential: str | None) -> str | None:
    """The decision, apart from the lookup. `phase` is `None` when the cluster has no row.

    `credential` is the digest of the token the session opened with. A session
    with none (a test, or a caller that never had one) is judged on the cluster
    alone.
    """
    if phase is None:
        return UNKNOWN
    if phase == "disabled":
        return DISABLED
    if credential is not None and token_hash != credential:
        return ROTATED
    return None


class DatabaseStanding:
    """Reads the cluster's registry row, one short session per question."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sessions = sessions

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._sessions is None:
            self._sessions = session_factory()
        return self._sessions

    async def refusal(self, cluster_uid: str, credential: str | None) -> str | None:
        async with self.sessions() as db:
            row = (
                await db.execute(select(Cluster.phase, Cluster.token_hash).where(cluster_ref_clause(cluster_uid)))
            ).first()
        if row is None:
            return judge(None, None, credential)
        return judge(row.phase, row.token_hash, credential)


class OpenStanding:
    """Admits every cluster. For tests and for a hub run with no registry behind it."""

    async def refusal(self, cluster_uid: str, credential: str | None) -> str | None:
        return None


class MemoryStanding:
    """A registry held in a dict, so a test can disable or rotate a cluster mid-session."""

    def __init__(self) -> None:
        self.refusals: dict[str, str] = {}

    async def refusal(self, cluster_uid: str, credential: str | None) -> str | None:
        return self.refusals.get(cluster_uid)
