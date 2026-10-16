"""The migration chain against a real Postgres: upgrade, drift, downgrade, and the data paths.

Each test gets a database of its own, because these drive alembic up and down and the
suite's shared database is expected to stay at head.
"""

import os
import uuid
import subprocess

from concurrent.futures import ThreadPoolExecutor

import pytest
import psycopg

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from alembic.migration import MigrationContext
from sqlalchemy.engine import make_url
from alembic.autogenerate import compare_metadata
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import models  # noqa: F401  (registers every table on the metadata)
from app.db.engine import Base
from app.core.exceptions import Conflict
from app.modules.quotas.schemas import QuotaCreateRequest
from app.modules.quotas.services import QuotaService

from tests.conftest import ROOT
from tests.conftest import drop
from tests.conftest import recreate
from tests.conftest import admin_url
from tests.conftest import scratch_name
from tests.test_migration_recipe_backfill import AFTER
from tests.test_migration_recipe_backfill import BEFORE
from tests.test_migration_recipe_backfill import _seed


pytestmark = pytest.mark.database


def _alembic(url: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, "HANGAR_DATABASE_URL": url},
    )


def _dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def scratch():
    """A database of its own, because these tests drive alembic up and down."""
    admin = admin_url()
    name = scratch_name(admin, "migrations")
    url = recreate(admin, name)
    yield url
    drop(admin, name)


def _include(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table" and obj.info.get("skip_autogenerate", False):
        return False
    return name != "_version"


def _drift(url: str) -> list:
    """What `alembic check` would report: every difference between the models and the schema."""
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"include_object": _include})
            return compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    with psycopg.connect(_dsn(url)) as conn:
        rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall()
    return {name for (name,) in rows}


def test_upgrade_from_empty_matches_the_models(scratch) -> None:
    _alembic(scratch, "upgrade", "head")
    assert _drift(scratch) == []


def test_every_seq_is_a_bigint_identity_that_does_not_wrap(scratch) -> None:
    _alembic(scratch, "upgrade", "head")
    with psycopg.connect(_dsn(scratch)) as conn:
        rows = conn.execute(
            "SELECT c.table_name, c.data_type, s.cycle"
            "  FROM information_schema.columns c"
            "  JOIN pg_sequences s ON s.sequencename = pg_get_serial_sequence(c.table_name, 'seq')::regclass::text"
            " WHERE c.table_schema = current_schema() AND c.column_name = 'seq'"
        ).fetchall()
    assert rows, "every table has a seq"
    assert {(data_type, cycle) for _, data_type, cycle in rows} == {("bigint", False)}


def test_downgrade_to_base_and_back(scratch) -> None:
    _alembic(scratch, "upgrade", "head")
    _alembic(scratch, "downgrade", "base")
    assert _tables(scratch) <= {"_version"}, "downgrade to base leaves nothing behind"
    _alembic(scratch, "upgrade", "head")
    assert _drift(scratch) == []


def test_two_concurrent_upgrades_apply_each_revision_once(scratch) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _alembic(scratch, "upgrade", "head", check=False), range(2)))
    assert [r.returncode for r in results] == [0, 0], [r.stderr[-2000:] for r in results]
    assert _drift(scratch) == []


def test_the_recipe_fold_downgrades_and_upgrades_with_data(scratch) -> None:
    _alembic(scratch, "upgrade", BEFORE)
    with psycopg.connect(_dsn(scratch), autocommit=True) as conn, conn.cursor() as cur:
        _seed(cur)
    _alembic(scratch, "upgrade", AFTER)
    with psycopg.connect(_dsn(scratch)) as conn:
        first = dict(conn.execute("SELECT uid, serving_digest FROM demand_workload").fetchall())

    _alembic(scratch, "downgrade", BEFORE)
    with psycopg.connect(_dsn(scratch)) as conn:
        recipes = conn.execute("SELECT count(*) FROM demand_recipe").fetchone()[0]
        unlinked = conn.execute(
            "SELECT count(*) FROM demand_workload w LEFT JOIN demand_recipe r ON r.id = w.recipe_id WHERE r.id IS NULL"
        ).fetchone()[0]
        run_recipe = conn.execute("SELECT recipe_id FROM ops_benchmark_run").fetchone()[0]
        lifecycle_index = conn.execute("SELECT to_regclass('ix_demand_recipe_lifecycle')").fetchone()[0]
    assert recipes == 3, "one recipe per workload"
    assert unlinked == 0
    assert run_recipe is not None, "the run is re-linked to a recipe with its configuration"
    assert lifecycle_index is not None

    _alembic(scratch, "upgrade", AFTER)
    with psycopg.connect(_dsn(scratch)) as conn:
        second = dict(conn.execute("SELECT uid, serving_digest FROM demand_workload").fetchall())
    assert second == first, "a round trip does not move any configuration's identity"


def test_the_recipe_fold_downgrade_links_runs_to_their_recipe_version(scratch) -> None:
    _alembic(scratch, "upgrade", BEFORE)
    with psycopg.connect(_dsn(scratch), autocommit=True) as conn, conn.cursor() as cur:
        _seed(cur)
    _alembic(scratch, "upgrade", AFTER)
    with psycopg.connect(_dsn(scratch), autocommit=True) as conn:
        # w1 holds the run. Give it an older namesake in another project so the downgrade
        # rebuilds it as version 2 of recipe "w1", and drop w2, the other workload with its digest.
        conn.execute("DELETE FROM demand_workload WHERE uid = 'wkl_w2'")
        tenant = conn.execute("SELECT tenant_id FROM bind_project WHERE slug = 'core'").fetchone()[0]
        other = conn.execute(
            "INSERT INTO bind_project (uid, tenant_id, name, slug) VALUES ('prj_o', %s, 'other', 'other') RETURNING id",
            (tenant,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE demand_workload SET project_id = %s, name = 'w1', created_at = now() - interval '1 day'"
            " WHERE uid = 'wkl_w3'",
            (other,),
        )

    _alembic(scratch, "downgrade", BEFORE)
    with psycopg.connect(_dsn(scratch)) as conn:
        linked = conn.execute(
            "SELECT b.recipe_version, r.version, r.name FROM ops_benchmark_run b JOIN demand_recipe r ON r.id = b.recipe_id"
        ).fetchall()
    assert linked == [(2, 2, "w1")]


def test_a_workload_with_no_deployment_stops_the_upgrade_unless_asked(scratch) -> None:
    before, after = "8f4ccc237d91", "e5b2c73a9d18"
    _alembic(scratch, "upgrade", before)
    with psycopg.connect(_dsn(scratch), autocommit=True) as conn:
        tenant = conn.execute(
            "INSERT INTO bind_tenant (uid, name, slug) VALUES ('ten_t', 't', 't') RETURNING id"
        ).fetchone()[0]
        project = conn.execute(
            "INSERT INTO bind_project (uid, tenant_id, name, slug) VALUES ('prj_t', %s, 'core', 'core') RETURNING id",
            (tenant,),
        ).fetchone()[0]
        priority = conn.execute("SELECT id FROM bind_priority_class LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO demand_workload (uid, kind, project_id, priority_class_id)"
            " VALUES ('wkl_orphan', 'service', %s, %s)",
            (project, priority),
        )

    refused = _alembic(scratch, "upgrade", after, check=False)
    assert refused.returncode != 0
    assert "wkl_orphan" in refused.stderr
    with psycopg.connect(_dsn(scratch)) as conn:
        assert conn.execute("SELECT count(*) FROM demand_workload").fetchone()[0] == 1

    _alembic(scratch, "-x", "drop_orphan_workloads=true", "upgrade", after)
    with psycopg.connect(_dsn(scratch)) as conn:
        assert conn.execute("SELECT count(*) FROM demand_workload").fetchone()[0] == 0


def _project(conn) -> uuid.UUID:
    tenant = conn.execute(
        "INSERT INTO bind_tenant (uid, name, slug) VALUES ('ten_t', 't', 't') RETURNING id"
    ).fetchone()[0]
    return conn.execute(
        "INSERT INTO bind_project (uid, tenant_id, name, slug) VALUES ('prj_t', %s, 'core', 'core') RETURNING id",
        (tenant,),
    ).fetchone()[0]


async def test_a_second_fleet_wide_quota_is_a_conflict(scratch) -> None:
    _alembic(scratch, "upgrade", "head")
    with psycopg.connect(_dsn(scratch), autocommit=True) as conn:
        _project(conn)

    engine = create_async_engine(scratch, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            request = QuotaCreateRequest(project="core", device_class="h100-80gb-sxm", guaranteed_devices=1)
            await QuotaService(db).create(request)
            with pytest.raises(Conflict):
                await QuotaService(db).create(request)
    finally:
        await engine.dispose()

    with psycopg.connect(_dsn(scratch)) as conn:
        assert conn.execute("SELECT count(*) FROM bind_quota WHERE cluster_id IS NULL").fetchone()[0] == 1


def test_a_second_fleet_wide_credential_name_is_refused(scratch) -> None:
    _alembic(scratch, "upgrade", "head")
    with psycopg.connect(_dsn(scratch), autocommit=True) as conn:
        insert = (
            "INSERT INTO ops_credential (name, kind, scope_type, scope_id, secret_ref)"
            " VALUES ('hf', 'hf_token', 'fleet', NULL, 'secret/hf')"
        )
        conn.execute(insert)
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert)


def test_benchmark_history_survives_its_deployment(scratch) -> None:
    _alembic(scratch, "upgrade", "head")
    with psycopg.connect(_dsn(scratch), autocommit=True) as conn:
        project = _project(conn)
        priority = conn.execute("SELECT id FROM bind_priority_class LIMIT 1").fetchone()[0]
        artifact = conn.execute(
            "INSERT INTO demand_artifact (uid, digest, name) VALUES ('art_t', 'sha256:aa', 'm') RETURNING id"
        ).fetchone()[0]
        cluster = conn.execute(
            "INSERT INTO supply_cluster (uid, name, region) VALUES ('clu_t', 'alpha', 'us-east-1') RETURNING id"
        ).fetchone()[0]
        workload = conn.execute(
            "INSERT INTO demand_workload (uid, name, kind, project_id, priority_class_id, artifact_id,"
            " serving_digest, engine, engine_runtime_version, context_length, device_class)"
            " VALUES ('wkl_t', 'w', 'service', %s, %s, %s, 'svg_x', 'vllm', '1.0.0', 4096, 'h100-80gb-sxm')"
            " RETURNING id",
            (project, priority, artifact),
        ).fetchone()[0]
        deployment = conn.execute(
            "INSERT INTO demand_deployment (uid, workload_id, cluster_id, name) VALUES ('dep_t', %s, %s, 'w')"
            " RETURNING id",
            (workload, cluster),
        ).fetchone()[0]
        request = conn.execute(
            "INSERT INTO ops_benchmark_request (uid, level, deployment_id, harness, criteria_version, requested_by)"
            " VALUES ('bmr_t', 'deployment', %s, 'aiperf', 'v1', 'tests') RETURNING id",
            (deployment,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO ops_benchmark_run (uid, request_id, deployment_id, cluster_id, harness, requested_by, model,"
            " serving_digest) VALUES ('bmk_t', %s, %s, %s, 'aiperf', 'tests', 'm', 'svg_x')",
            (request, deployment, cluster),
        )

        conn.execute("DELETE FROM demand_deployment WHERE id = %s", (deployment,))
        conn.execute("DELETE FROM demand_workload WHERE id = %s", (workload,))

        run = conn.execute("SELECT deployment_id, serving_digest, request_id FROM ops_benchmark_run").fetchall()
        requests = conn.execute("SELECT deployment_id, workload_id FROM ops_benchmark_request").fetchall()
    assert run == [(None, "svg_x", request)]
    assert requests == [(None, None)]


def test_deleting_a_subject_deletes_its_policy_attachments(scratch) -> None:
    _alembic(scratch, "upgrade", "ee98c5cc5d50")
    with psycopg.connect(_dsn(scratch), autocommit=True) as conn:
        project = _project(conn)
        tenant = conn.execute("SELECT tenant_id FROM bind_project WHERE id = %s", (project,)).fetchone()[0]
        policy = conn.execute("INSERT INTO bind_policy (uid, name) VALUES ('plc_t', 'p') RETURNING id").fetchone()[0]
        attach = "INSERT INTO bind_policy_attachment (policy_id, subject_type, subject_id) VALUES (%s, %s, %s)"
        conn.execute(attach, (policy, "tenant", tenant))
        conn.execute(attach, (policy, "project", project))
        conn.execute(attach, (policy, "cluster", uuid.uuid4()))

    _alembic(scratch, "upgrade", "head")
    attached = "SELECT subject_type FROM bind_policy_attachment ORDER BY subject_type"
    with psycopg.connect(_dsn(scratch), autocommit=True) as conn:
        assert [t for (t,) in conn.execute(attached).fetchall()] == ["project", "tenant"], "the orphan is gone"
        conn.execute("DELETE FROM bind_tenant WHERE id = %s", (tenant,))
        assert conn.execute(attached).fetchall() == [], "the cascaded project took its attachment too"
