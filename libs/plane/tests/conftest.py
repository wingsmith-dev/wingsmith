"""Fixtures shared by the database-backed suites.

The migrated test database and the session factory live here rather than in one test
module, so a second suite that needs them imports nothing: pytest fixtures crossing
module boundaries by import shadow their own parameters, and the linter is right to
complain about it.
"""

import os
import uuid
import subprocess

from pathlib import Path

import pytest
import psycopg

from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine


ROOT = Path(__file__).resolve().parents[1]

ADMIN_URL = os.environ.get("HANGAR_TEST_DATABASE_URL", "postgresql+psycopg://hangar:hangar@localhost:5442/hangar")
TEST_DATABASE = "hangar_ingestion_test"

CLUSTER = "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(scope="session")
def database() -> str:
    """A freshly migrated test database, or a skip when nothing is listening."""
    admin = make_url(ADMIN_URL)
    dsn = admin.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        connection = psycopg.connect(dsn, connect_timeout=3, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"no database at {admin.host}:{admin.port} — run `just dev-up` ({exc})")

    with connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{TEST_DATABASE}"')

    url = admin.set(database=TEST_DATABASE).render_as_string(hide_password=False)
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env={**os.environ, "HANGAR_DATABASE_URL": url},
    )
    return url


@pytest.fixture
async def sessions(database: str):
    engine = create_async_engine(database, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE supply_cluster RESTART IDENTITY CASCADE"))
        await connection.execute(text("TRUNCATE ops_event, ops_operation RESTART IDENTITY CASCADE"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def cluster(sessions) -> uuid.UUID:
    return await seed_cluster(sessions, CLUSTER, "alpha")


async def seed_cluster(sessions, uid: str, name: str) -> uuid.UUID:
    async with sessions.begin() as db:
        result = await db.execute(
            text("INSERT INTO supply_cluster (uid, name, region) VALUES (:uid, :name, 'eu-west-1') RETURNING id"),
            {"uid": uid, "name": name},
        )
        return result.scalar_one()


async def fetch(sessions, sql: str, params=None) -> list:
    async with sessions() as db:
        result = await db.execute(text(sql), params or {})
        return result.all()
