"""The endpoint-slug unique index refuses to build over existing duplicates, and says which."""

import os
import subprocess

import pytest
import psycopg

from sqlalchemy.engine import make_url

from tests.conftest import ROOT
from tests.conftest import drop
from tests.conftest import recreate
from tests.conftest import admin_url
from tests.conftest import scratch_name


pytestmark = pytest.mark.database

BEFORE = "5a8c3e1f7b24"
AFTER = "6b9d4f2a8c35"


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "WINGSMITH_DATABASE_URL": url},
    )


@pytest.fixture
def scratch():
    admin = admin_url()
    name = scratch_name(admin, "migration")
    url = recreate(admin, name)
    yield url
    drop(admin, name)


def _connect(url: str):
    return psycopg.connect(make_url(url).set(drivername="postgresql").render_as_string(hide_password=False))


def test_duplicate_slugs_stop_the_upgrade_by_name(scratch) -> None:
    assert _alembic(scratch, "upgrade", BEFORE).returncode == 0
    with _connect(scratch) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bind_tenant (id, uid, name, slug) VALUES (gen_random_uuid(), 'tnt_t', 't', 't') RETURNING id"
        )
        tenant = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO bind_project (id, uid, tenant_id, name, slug)"
            " VALUES (gen_random_uuid(), 'prj_t', %s, 'core', 'core') RETURNING id",
            (tenant,),
        )
        project = cur.fetchone()[0]
        for uid, name in (("end_a", "a"), ("end_b", "b")):
            cur.execute(
                "INSERT INTO demand_endpoint (id, uid, project_id, name, slug)"
                " VALUES (gen_random_uuid(), %s, %s, %s, 'gpt-oss')",
                (uid, project, name),
            )
    failed = _alembic(scratch, "upgrade", AFTER)
    assert failed.returncode != 0
    assert "'gpt-oss': end_a, end_b" in failed.stderr

    with _connect(scratch) as conn, conn.cursor() as cur:
        cur.execute("UPDATE demand_endpoint SET slug = 'gpt-oss-b' WHERE uid = 'end_b'")
    assert _alembic(scratch, "upgrade", AFTER).returncode == 0


def test_an_alias_equal_to_another_endpoints_slug_stops_the_upgrade_by_name(scratch) -> None:
    assert _alembic(scratch, "upgrade", "7c2e5b9d1a46").returncode == 0
    with _connect(scratch) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bind_tenant (id, uid, name, slug) VALUES (gen_random_uuid(), 'tnt_t', 't', 't') RETURNING id"
        )
        tenant = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO bind_project (id, uid, tenant_id, name, slug)"
            " VALUES (gen_random_uuid(), 'prj_t', %s, 'core', 'core') RETURNING id",
            (tenant,),
        )
        project = cur.fetchone()[0]
        ids = {}
        for uid, slug in (("end_a", "gpt-oss"), ("end_b", "other")):
            cur.execute(
                "INSERT INTO demand_endpoint (id, uid, project_id, name, slug)"
                " VALUES (gen_random_uuid(), %s, %s, %s, %s) RETURNING id",
                (uid, project, uid, slug),
            )
            ids[uid] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO demand_endpoint_alias (id, endpoint_id, alias) VALUES (gen_random_uuid(), %s, 'gpt-oss')",
            (ids["end_b"],),
        )
    failed = _alembic(scratch, "upgrade", "8d3f6c0e2b57")
    assert failed.returncode != 0
    assert "'gpt-oss': alias of end_b, slug of end_a" in failed.stderr

    with _connect(scratch) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM demand_endpoint_alias")
    assert _alembic(scratch, "upgrade", "8d3f6c0e2b57").returncode == 0
