"""The recipe-to-workload backfill, run against a database that actually has recipes.

This file exists because the ordinary harness cannot catch what it is testing. Every
other database test starts from `alembic upgrade head` on an EMPTY database, so
`a7c31f8e64b2`'s backfill found nothing, took its `if digests:` guard, and never
executed a statement. The migration reached production with its two `UPDATE`s never
once having run, and failed there on `function unnest(unknown) is not unique`.

So this migrates to the revision BEFORE it, writes rows the way the old schema
wanted them, and then upgrades — which is the only arrangement in which the backfill
has anything to do.
"""

import os
import uuid
import subprocess

import pytest
import psycopg

from sqlalchemy.engine import make_url

from tests.conftest import ROOT
from tests.conftest import ADMIN_URL


pytestmark = pytest.mark.database

BEFORE = "d8f1a4c96b23"
AFTER = "a7c31f8e64b2"
SCRATCH = "hangar_migration_test"


def _alembic(url: str, *args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env={**os.environ, "HANGAR_DATABASE_URL": url},
    )


@pytest.fixture
def scratch() -> str:
    """A database of its own, because this test drives alembic rather than reading rows.

    It cannot share the suite's database: that one is migrated to head once per
    session and every other test expects it to stay there.
    """
    admin = make_url(ADMIN_URL)
    dsn = admin.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        connection = psycopg.connect(dsn, connect_timeout=3, autocommit=True)
    except psycopg.Error as exc:
        pytest.skip(f"no database at {admin.host}:{admin.port} ({exc})")
    with connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{SCRATCH}"')
    url = admin.set(database=SCRATCH).render_as_string(hide_password=False)
    yield url
    with psycopg.connect(dsn, connect_timeout=3, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)')


def _seed(cur) -> dict[str, uuid.UUID]:
    """One artifact, two recipes, three workloads and a benchmark run, pre-migration.

    Two workloads share a recipe — the canary case, and the only shape in which the
    old sharing bought anything — so the backfill has to give them the same digest.
    """
    ids = {name: uuid.uuid4() for name in ("artifact", "rec_a", "rec_b", "w1", "w2", "w3", "run")}
    cur.execute(
        "INSERT INTO demand_artifact (id, uid, name, kind, digest, format, size_bytes, licence)"
        " VALUES (%s, 'art_test', 'meta/llama-70b', 'weights', 'sha256:aa', 'safetensors', 1, 'mit')",
        (ids["artifact"],),
    )
    for key, name, tp in (("rec_a", "llama-tp2", 2), ("rec_b", "llama-tp4", 4)):
        cur.execute(
            "INSERT INTO demand_recipe (id, uid, artifact_id, name, version, engine, engine_version,"
            " context_length, device_class, device_count, tensor_parallel, kv_cache_fraction)"
            " VALUES (%s, %s, %s, %s, 1, 'vllm', '0.11.0', 32768, 'h100-80gb-sxm', %s, %s, 0.9)",
            (ids[key], f"rec_{key}", ids["artifact"], name, tp, tp),
        )
    cur.execute("SELECT id FROM bind_tenant LIMIT 1")
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO bind_tenant (id, uid, name, slug) VALUES (gen_random_uuid(), 'tnt_t', 't', 't') RETURNING id"
        )
        row = cur.fetchone()
    tenant = row[0]

    cur.execute("SELECT id FROM bind_project LIMIT 1")
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO bind_project (id, uid, tenant_id, name, slug)"
            " VALUES (gen_random_uuid(), 'prj_t', %s, 'core', 'core') RETURNING id",
            (tenant,),
        )
        row = cur.fetchone()
    project = row[0]

    cur.execute("SELECT id FROM bind_priority_class LIMIT 1")
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO bind_priority_class (id, uid, name, value)"
            " VALUES (gen_random_uuid(), 'pcl_t', 'standard', 100) RETURNING id"
        )
        row = cur.fetchone()
    priority = row[0]

    for key, recipe in (("w1", "rec_a"), ("w2", "rec_a"), ("w3", "rec_b")):
        cur.execute(
            "INSERT INTO demand_workload (id, uid, name, kind, project_id, priority_class_id, recipe_id)"
            " VALUES (%s, %s, %s, 'service', %s, %s, %s)",
            (ids[key], f"wkl_{key}", key, project, priority, ids[recipe]),
        )
    cur.execute(
        "INSERT INTO supply_cluster (id, uid, name, region)"
        " VALUES (gen_random_uuid(), 'clu_t', 'alpha', 'us-east-1') RETURNING id"
    )
    cluster = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO demand_deployment (id, uid, workload_id, cluster_id, name)"
        " VALUES (gen_random_uuid(), 'dep_t', %s, %s, 'w1') RETURNING id",
        (ids["w1"], cluster),
    )
    deployment = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO ops_benchmark_run (id, uid, harness, requested_by, model, deployment_id,"
        " recipe_id, recipe_version)"
        " VALUES (%s, 'bmr_t', 'aiperf', 'tests', 'meta/llama-70b', %s, %s, 1)",
        (ids["run"], deployment, ids["rec_a"]),
    )
    return ids


def test_the_backfill_runs_and_agrees_with_the_plane(scratch) -> None:
    """The statement that failed in production, executed against rows that make it run.

    Asserted on more than "it did not raise": the two workloads that shared a recipe
    must land on ONE digest, the third on a different one, and the benchmark run must
    carry the digest of the recipe it measured. A backfill that wrote a constant, or
    wrote the wrong row's digest, passes a smoke test and fails all three of these.
    """
    _alembic(scratch, "upgrade", BEFORE)
    dsn = make_url(scratch).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        ids = _seed(cur)

    _alembic(scratch, "upgrade", AFTER)

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT uid, serving_digest, artifact_id, context_length FROM demand_workload ORDER BY uid")
        rows = {uid: (digest, artifact, ctx) for uid, digest, artifact, ctx in cur.fetchall()}
        cur.execute("SELECT serving_digest FROM ops_benchmark_run")
        run_digest = cur.fetchone()[0]
        cur.execute("SELECT to_regclass('demand_recipe')")
        recipe_table = cur.fetchone()[0]

    assert all(digest for digest, _, _ in rows.values()), "every workload must carry a digest"
    assert all(digest.startswith("svg_") and len(digest) == 68 for digest, _, _ in rows.values())

    assert rows["wkl_w1"][0] == rows["wkl_w2"][0], "two workloads over one recipe are one configuration"
    assert rows["wkl_w3"][0] != rows["wkl_w1"][0], "a different tensor degree is a different configuration"

    assert run_digest == rows["wkl_w1"][0], "the run must carry the digest of the recipe it measured"

    assert all(artifact == ids["artifact"] for _, artifact, _ in rows.values())
    assert all(ctx == 32768 for _, _, ctx in rows.values()), "the moved columns carry their values"
    assert recipe_table is None, "demand_recipe is dropped once nothing needs it"


def test_the_backfilled_digest_is_the_one_the_plane_would_compute(scratch) -> None:
    """A migration that hashes differently from the application is worse than no digest.

    It would split one configuration into two populations silently — the rows would
    look fine, and only a benchmark rollup would quietly report half of what it had.
    """
    from types import SimpleNamespace

    from app.core.serving import serving_digest

    _alembic(scratch, "upgrade", BEFORE)
    dsn = make_url(scratch).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        _seed(cur)
    _alembic(scratch, "upgrade", AFTER)

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT w.serving_digest, a.uid, w.engine, w.engine_version, w.engine_image, w.frontend_image,"
            " w.engine_args, w.quantization, w.dtype, w.tensor_parallel, w.pipeline_parallel, w.expert_parallel,"
            " w.context_length, w.max_batch_tokens, w.kv_cache_dtype, w.kv_cache_fraction, w.prefix_cache_enabled,"
            " w.prefix_cache_config, w.device_class, w.device_count, w.device_memory_mib_min, w.cpu_cores,"
            " w.memory_mib, w.disk_gib, w.overlay, w.serving"
            " FROM demand_workload w JOIN demand_artifact a ON a.id = w.artifact_id WHERE w.uid = 'wkl_w1'"
        )
        row = cur.fetchone()

    stored, artifact_uid = row[0], row[1]
    # The SELECT above speaks the schema at `AFTER`, which predates the rename of
    # `engine_version` to `engine_runtime_version`; these names are today's attributes,
    # because `serving_digest` reads them off the object. That the two differ and the
    # digest still matches is the point: the hash keys are frozen, so renaming a column
    # does not move the identity of a configuration.
    names = (
        "engine",
        "engine_runtime_version",
        "engine_runtime_image",
        "frontend_image",
        "engine_args",
        "quantization",
        "dtype",
        "tensor_parallel",
        "pipeline_parallel",
        "expert_parallel",
        "context_length",
        "max_batch_tokens",
        "kv_cache_dtype",
        "kv_cache_fraction",
        "prefix_cache_enabled",
        "prefix_cache_config",
        "device_class",
        "device_count",
        "device_memory_mib_min",
        "cpu_cores",
        "memory_mib",
        "disk_gib",
        "overlay",
        "serving",
    )
    workload = SimpleNamespace(**dict(zip(names, row[2:])))
    assert stored == serving_digest(workload, SimpleNamespace(uid=artifact_uid))
