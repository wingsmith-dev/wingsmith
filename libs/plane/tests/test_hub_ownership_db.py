"""`app.hub.ownership` against a real Postgres (ADR 0005).

The claim is a single `INSERT ... ON CONFLICT DO UPDATE`, and the property that matters
is one only a real database can show: N concurrent claimants for one `cluster_id` leave
exactly one winner in the row, never a torn or duplicated one. `test_hub_session.py`
covers the session-level behaviour (displacement, self-eviction) against
`MemoryClaimStore`; this file covers the SQL.
"""

import asyncio

import pytest

from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.hub.state import new_id
from app.hub.ownership import DatabaseClaimStore
from app.hub.ownership import ClusterClaimService


pytestmark = pytest.mark.database

CLUSTER = "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
async def sessions(database: str):
    engine = create_async_engine(database, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE hub_cluster_claim"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_claim_is_atomic_under_concurrent_claims(sessions) -> None:
    store = DatabaseClaimStore(sessions)
    session_ids = [new_id() for _ in range(16)]

    await asyncio.gather(*(store.claim(CLUSTER, sid, f"pod-{i}") for i, sid in enumerate(session_ids)))

    async with sessions() as db:
        result = await db.execute(text("SELECT count(*) FROM hub_cluster_claim WHERE cluster_id = :c"), {"c": CLUSTER})
        assert result.scalar_one() == 1, "one row per cluster_id no matter how many claimants raced for it"

    winners = [sid for sid in session_ids if await store.owns(CLUSTER, sid)]
    assert len(winners) == 1


async def test_a_newer_claim_displaces_the_older_one(sessions) -> None:
    store = DatabaseClaimStore(sessions)
    first, second = new_id(), new_id()

    await store.claim(CLUSTER, first, "pod-a")
    assert await store.owns(CLUSTER, first)

    await store.claim(CLUSTER, second, "pod-b")
    assert await store.owns(CLUSTER, second)
    assert not await store.owns(CLUSTER, first)


async def test_owns_is_false_for_a_cluster_never_claimed(sessions) -> None:
    store = DatabaseClaimStore(sessions)
    assert not await store.owns(CLUSTER, new_id())


async def test_service_claim_commits_and_service_owns_reads_it_back(sessions) -> None:
    async with sessions() as db:
        service = ClusterClaimService(db)
        session_id = new_id()
        await service.claim(CLUSTER, session_id, "pod-a")

    async with sessions() as db:
        assert await ClusterClaimService(db).owns(CLUSTER, session_id)
