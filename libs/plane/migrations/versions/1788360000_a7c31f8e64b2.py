"""recipe is folded into the workload that names it

Revision ID: a7c31f8e64b2
Revises: d8f1a4c96b23
Create Date: 2026-09-02 14:10:00.000000

`Recipe` existed so that one serving configuration could be shared by several
workloads and travel to a cluster once. Neither half held.

The wire half is measurable: `ingestion/desired.py` selected recipes joined through
the deployments of one cluster, so a configuration used in three clusters was
serialised three times regardless. The only case that saved a frame was two
deployments in ONE cluster naming one recipe — two workloads over one configuration,
both placed together, which is the canary case and nothing else. Against that the
separate frame cost an ordering rule, a `RecipeNotDeclared` pending reason, and a
resume race in which a deployment waited forever for a recipe it was never sent.

The sharing half was already being reconstructed rather than used: the console's
`matchExistingRecipe` compared eight fields to decide whether an existing recipe was
the same one, because a person deploying the same model twice must not mint a second
identity for it. Comparing contents to find an object says the contents are the
object, so the reuse becomes `serving_digest` and the object goes away.

Nothing about what the plane knows changes. Every column lands on `demand_workload`
under the same name and type, placement reads the shape off the workload instead of
its recipe, and the shape still reaches a cluster on the deployment's `placement`
rather than in the serving block.

Two columns merge rather than move. `requires_single_topology_domain` and
`topology_domain_kind` existed on both — non-null on the recipe, nullable on the
workload as an override of it — and become one non-null answer, backfilled
`COALESCE(workload, recipe)` so a workload that overrode keeps its override.

`ops_benchmark_run.recipe_id` and `recipe_version` become `serving_digest`. The old
pair named a mutable row, which is why the table already snapshotted `engine_args`
beside it; a digest is the configuration rather than a handle on it. Every run is
backfilled from its own recipe, so attribution survives even for runs whose recipe no
workload ever named.

`Recipe.name`, `Recipe.version` and `Recipe.lifecycle` are dropped and not replaced. A
workload has a name; a serving configuration no longer needs one of its own.

The digest backfill uses a frozen copy of `serving_digest` as it was when this revision
was written, not the live function: a migration that imports application code breaks
whenever that code grows a field the schema at this point in history does not have.

`downgrade()` rebuilds the table and the columns, but not the names or the sharing:
every workload gets a recipe of its own, named after the workload, at lifecycle
`published`. Nothing after this migration records what a recipe was called or which
workloads shared it.

"""


import json
import hashlib

from uuid import UUID
from typing import Any
from typing import Union
from decimal import Decimal
from typing import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a7c31f8e64b2"
down_revision: Union[str, None] = "d8f1a4c96b23"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MOVED = (
    ("engine", sa.String(length=64), False, None),
    ("engine_version", sa.String(length=64), False, None),
    ("engine_image", sa.String(length=512), True, None),
    ("frontend_image", sa.String(length=512), True, None),
    ("engine_args", postgresql.JSONB(astext_type=sa.Text()), False, "'{}'::jsonb"),
    ("imported_args", postgresql.JSONB(astext_type=sa.Text()), False, "'{}'::jsonb"),
    ("quantization", sa.String(length=32), True, None),
    ("dtype", sa.String(length=32), True, None),
    ("tensor_parallel", sa.Integer(), False, "1"),
    ("pipeline_parallel", sa.Integer(), False, "1"),
    ("expert_parallel", sa.Integer(), False, "1"),
    ("context_length", sa.Integer(), False, None),
    ("max_batch_tokens", sa.Integer(), True, None),
    ("kv_cache_dtype", sa.String(length=32), True, None),
    ("kv_cache_fraction", sa.Numeric(5, 4), True, None),
    ("prefix_cache_enabled", sa.Boolean(), False, "true"),
    ("prefix_cache_config", postgresql.JSONB(astext_type=sa.Text()), False, "'{}'::jsonb"),
    ("device_class", sa.String(length=64), False, None),
    ("device_count", sa.Integer(), False, "1"),
    ("device_memory_mib_min", sa.Integer(), False, "0"),
    ("cpu_cores", sa.Integer(), True, None),
    ("memory_mib", sa.BigInteger(), True, None),
    ("disk_gib", sa.Integer(), True, None),
    ("overlay", postgresql.JSONB(astext_type=sa.Text()), False, "'{}'::jsonb"),
    ("serving", postgresql.JSONB(astext_type=sa.Text()), False, "'{}'::jsonb"),
    ("capabilities", postgresql.JSONB(astext_type=sa.Text()), False, "'{}'::jsonb"),
)
"""Every recipe column that moves, with the nullability and default it lands under.

Added nullable whatever the target says, backfilled, then tightened — a table with
rows in it cannot take a non-null column in one step, and inventing a default to make
it possible would write a value nobody chose into every existing workload.
"""

DIGEST_FIELDS = (
    "engine",
    "engine_version",
    "engine_image",
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
"""The digest's hash keys at this revision. They are also the `demand_recipe` column names
here, so the row can be read by name. The live digest still hashes under these keys."""

DIGEST_SOURCE = f"""
    SELECT r.id AS recipe_id, a.uid AS artifact_uid, {", ".join("r." + name for name in DIGEST_FIELDS)}
      FROM demand_recipe r
      JOIN demand_artifact a ON a.id = r.artifact_id
"""


def _canonical(value: Any) -> Any:
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def serving_digest(row: Any) -> str:
    """Frozen copy of `app.core.serving.serving_digest` as of this revision.

    `svg_` + sha256 over canonical JSON of the artifact uid and `DIGEST_FIELDS`. Every
    recipe at this point is aggregated and declares no router, and for those the live
    function produces the same digest.
    """
    payload: dict[str, Any] = {"artifact": str(row.artifact_uid)}
    for name in DIGEST_FIELDS:
        payload[name] = _canonical(getattr(row, name))
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "svg_" + hashlib.sha256(body.encode("utf-8")).hexdigest()


BACKFILL = """
    UPDATE {table} t
       SET serving_digest = d.digest
      FROM unnest(CAST(:ids AS uuid[]), CAST(:digests AS text[])) AS d(recipe_id, digest)
     WHERE d.recipe_id = t.recipe_id
"""
"""Both backfills, which differ only in the table they write. The casts are required:
a bare `unnest(:ids)` is an untyped parameter and Postgres cannot pick an overload."""


def _digests(bind: sa.engine.Connection) -> dict[str, str]:
    """`{recipe_id: serving_digest}` for every recipe."""
    return {str(row.recipe_id): serving_digest(row) for row in bind.execute(sa.text(DIGEST_SOURCE))}


def upgrade() -> None:
    bind = op.get_bind()

    for name, kind, _nullable, _default in MOVED:
        op.add_column("demand_workload", sa.Column(name, kind, nullable=True))
    op.add_column("demand_workload", sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("demand_workload", sa.Column("serving_digest", sa.String(length=71), nullable=True))

    assignments = ", ".join(f"{name} = r.{name}" for name, _k, _n, _d in MOVED)
    bind.execute(
        sa.text(
            f"""
            UPDATE demand_workload w
               SET {assignments},
                   artifact_id = r.artifact_id,
                   requires_single_topology_domain =
                       COALESCE(w.requires_single_topology_domain, r.requires_single_topology_domain),
                   topology_domain_kind =
                       COALESCE(w.topology_domain_kind, r.topology_domain_kind)
              FROM demand_recipe r
             WHERE r.id = w.recipe_id
            """
        )
    )

    digests = _digests(bind)
    if digests:
        bind.execute(
            sa.text(BACKFILL.format(table="demand_workload")),
            {"ids": list(digests.keys()), "digests": list(digests.values())},
        )

    for name, kind, nullable, default in MOVED:
        if not nullable:
            if default is not None:
                bind.execute(sa.text(f"UPDATE demand_workload SET {name} = {default} WHERE {name} IS NULL"))
            op.alter_column("demand_workload", name, existing_type=kind, nullable=False)
            if default is not None:
                op.alter_column("demand_workload", name, existing_type=kind, server_default=sa.text(default))

    op.alter_column(
        "demand_workload",
        "requires_single_topology_domain",
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    )
    op.alter_column("demand_workload", "artifact_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.alter_column("demand_workload", "serving_digest", existing_type=sa.String(length=71), nullable=False)

    op.create_index("ix_demand_workload_artifact_id", "demand_workload", ["artifact_id"])
    op.create_index("ix_demand_workload_device_class", "demand_workload", ["device_class"])
    op.create_index("ix_demand_workload_serving_digest", "demand_workload", ["serving_digest"])
    op.create_foreign_key(
        "fk_demand_workload_artifact_id",
        "demand_workload",
        "demand_artifact",
        ["artifact_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column("ops_benchmark_run", sa.Column("serving_digest", sa.String(length=71), nullable=True))
    if digests:
        bind.execute(
            sa.text(BACKFILL.format(table="ops_benchmark_run")),
            {"ids": list(digests.keys()), "digests": list(digests.values())},
        )
    op.create_index("ix_ops_benchmark_run_serving_digest", "ops_benchmark_run", ["serving_digest"])
    op.drop_column("ops_benchmark_run", "recipe_version")
    op.drop_column("ops_benchmark_run", "recipe_id")

    op.drop_column("demand_workload", "recipe_id")
    op.drop_table("demand_recipe")


def downgrade() -> None:
    """Rebuilds `demand_recipe` as it stood at `d8f1a4c96b23`, one recipe per workload."""
    bind = op.get_bind()

    op.create_table(
        "demand_recipe",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("uid", sa.String(length=40), nullable=False, unique=True),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lifecycle", sa.String(length=32), nullable=False, server_default="published"),
        sa.Column("served_model_name", sa.String(length=255), nullable=True),
        sa.Column("requires_single_topology_domain", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("topology_domain_kind", sa.String(length=32), nullable=True),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        *[
            sa.Column(name, kind, nullable=nullable, server_default=sa.text(default) if default else None)
            for name, kind, nullable, default in MOVED
        ],
        sa.ForeignKeyConstraint(["artifact_id"], ["demand_artifact.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("name", "version", name="uq_demand_recipe_name_version"),
    )
    op.create_index("ix_demand_recipe_artifact_id", "demand_recipe", ["artifact_id"])
    op.create_index("ix_demand_recipe_device_class", "demand_recipe", ["device_class"])
    op.create_index("ix_demand_recipe_lifecycle", "demand_recipe", ["lifecycle"])

    # The recipe id is minted on the workload first, so the insert below needs no join
    # back by name, and two workloads with one name in different projects become two
    # versions of one recipe name rather than a unique-constraint violation.
    op.add_column("demand_workload", sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=True))
    bind.execute(sa.text("UPDATE demand_workload SET recipe_id = gen_random_uuid()"))

    columns = ", ".join(name for name, _k, _n, _d in MOVED)
    bind.execute(
        sa.text(
            f"""
            INSERT INTO demand_recipe (
                id, uid, artifact_id, name, version, lifecycle, served_model_name,
                requires_single_topology_domain, topology_domain_kind, {columns}
            )
            SELECT w.recipe_id,
                   'rec_' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 26),
                   w.artifact_id, w.name,
                   row_number() OVER (PARTITION BY w.name ORDER BY w.created_at, w.id),
                   'published', w.served_model_name,
                   w.requires_single_topology_domain, w.topology_domain_kind, {columns}
              FROM demand_workload w
            """
        )
    )
    op.alter_column("demand_workload", "recipe_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.create_index("ix_demand_workload_recipe_id", "demand_workload", ["recipe_id"])
    op.create_foreign_key(
        "demand_workload_recipe_id_fkey",
        "demand_workload",
        "demand_recipe",
        ["recipe_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column("ops_benchmark_run", sa.Column("recipe_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("ops_benchmark_run", sa.Column("recipe_version", sa.Integer(), nullable=True))
    bind.execute(
        sa.text(
            """
            UPDATE ops_benchmark_run b
               SET recipe_id = w.recipe_id, recipe_version = 1
              FROM demand_workload w
             WHERE w.serving_digest = b.serving_digest
            """
        )
    )
    op.create_index("ix_ops_benchmark_run_recipe_id", "ops_benchmark_run", ["recipe_id"])
    op.create_foreign_key(
        "ops_benchmark_run_recipe_id_fkey",
        "ops_benchmark_run",
        "demand_recipe",
        ["recipe_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_index("ix_ops_benchmark_run_serving_digest", table_name="ops_benchmark_run")
    op.drop_column("ops_benchmark_run", "serving_digest")

    op.drop_constraint("fk_demand_workload_artifact_id", "demand_workload", type_="foreignkey")
    op.drop_index("ix_demand_workload_serving_digest", table_name="demand_workload")
    op.drop_index("ix_demand_workload_device_class", table_name="demand_workload")
    op.drop_index("ix_demand_workload_artifact_id", table_name="demand_workload")
    op.drop_column("demand_workload", "serving_digest")
    op.drop_column("demand_workload", "artifact_id")
    for name, _kind, _nullable, _default in MOVED:
        op.drop_column("demand_workload", name)
    op.alter_column(
        "demand_workload",
        "requires_single_topology_domain",
        existing_type=sa.Boolean(),
        nullable=True,
        server_default=None,
    )
