"""Fixtures for the database-backed suites (`-m database`).

They need `WINGSMITH_TEST_DATABASE_URL`, a Postgres URL with rights to create databases; `just
test-db` sets it. Nothing here touches the database that URL names: the schema is migrated once
into `wingsmith_test_template`, and each test module gets its own copy, dropped when it finishes.
`WINGSMITH_TEST_DATABASE_SUFFIX` goes after `wingsmith_test_` in every one of those names, so two runs
can share a server.
Without the variable, or with no server behind it, a database test fails instead of skipping.
"""

import os
import re
import uuid
import subprocess


# The plane defaults to `production`, which refuses to boot without its secrets. The suite is a
# developer's checkout, and tests that need `production` behaviour patch it in themselves. Set before
# anything imports `app.config`, which reads the environment once.
os.environ.setdefault("WINGSMITH_APP_ENV", "local")

from pathlib import Path

import pytest
import psycopg

from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import URL
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine


ROOT = Path(__file__).resolve().parents[1]

DATABASE_URL_VAR = "WINGSMITH_TEST_DATABASE_URL"
SCRATCH_PREFIX = "wingsmith_test_"
# Two checkouts running the suite against one server would drop each other's databases; a
# suffix keeps each run's scratch names apart. They all still start with SCRATCH_PREFIX.
DATABASE_PREFIX = SCRATCH_PREFIX + re.sub(
    r"[^a-z0-9_]", "_", os.environ.get("WINGSMITH_TEST_DATABASE_SUFFIX", "").lower()
)
TEMPLATE_DATABASE = DATABASE_PREFIX + "template"

CLUSTER = "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"


def admin_url() -> URL:
    """The server the database suites run against, from the environment and nowhere else."""
    raw = os.environ.get(DATABASE_URL_VAR, "").strip()
    if not raw:
        pytest.fail(f"database tests need {DATABASE_URL_VAR}; `just test-db` sets it", pytrace=False)
    return make_url(raw)


def admin_connection(admin: URL) -> psycopg.Connection:
    dsn = admin.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        return psycopg.connect(dsn, connect_timeout=3, autocommit=True)
    except psycopg.Error as exc:
        pytest.fail(f"no database at {admin.host}:{admin.port} ({exc})", pytrace=False)


def scratch_name(admin: URL, suffix: str) -> str:
    """A database name this harness owns, so creating and dropping it can never hit a real one."""
    name = DATABASE_PREFIX + re.sub(r"[^a-z0-9_]", "_", suffix.lower())
    name = name[:63]
    if name == admin.database:
        pytest.fail(f"{DATABASE_URL_VAR} names {name}, which the test suite drops; point it elsewhere", pytrace=False)
    return name


def recreate(admin: URL, name: str, template: str | None = None) -> str:
    """Drops and creates one scratch database and returns its URL."""
    assert name.startswith(SCRATCH_PREFIX)
    with admin_connection(admin) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        source = f' TEMPLATE "{template}"' if template else ""
        connection.execute(f'CREATE DATABASE "{name}"{source}')
    return admin.set(database=name).render_as_string(hide_password=False)


def drop(admin: URL, name: str) -> None:
    assert name.startswith(SCRATCH_PREFIX)
    with admin_connection(admin) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def database_template():
    """The migrated schema every module's database is copied from."""
    admin = admin_url()
    url = recreate(admin, scratch_name(admin, "template"))
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env={**os.environ, "WINGSMITH_DATABASE_URL": url},
    )
    yield admin
    drop(admin, TEMPLATE_DATABASE)


@pytest.fixture(scope="module")
def database(request, database_template: URL):
    """A migrated database of this test module's own, including the rows migrations seed."""
    admin = database_template
    name = scratch_name(admin, request.module.__name__.rsplit(".", 1)[-1])
    url = recreate(admin, name, template=TEMPLATE_DATABASE)
    yield url
    drop(admin, name)


@pytest.fixture
async def db(database: str):
    """One session on a database emptied of every row, migration seeds included."""
    engine = create_async_engine(database, poolclass=NullPool)
    async with engine.begin() as connection:
        tables = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
        )
        names = ", ".join(f'"{row[0]}"' for row in tables)
        if names:
            await connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
async def sessions(database: str):
    """A session factory on a database cleared of clusters, operations, events and alerts."""
    engine = create_async_engine(database, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE supply_cluster RESTART IDENTITY CASCADE"))
        await connection.execute(text("TRUNCATE ops_event, ops_operation RESTART IDENTITY CASCADE"))
        await connection.execute(
            text("TRUNCATE ops_alert, ops_notification_channel, ops_reactor_state RESTART IDENTITY CASCADE")
        )
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
