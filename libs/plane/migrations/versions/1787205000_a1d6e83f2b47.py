"""benchmark runs: what a deployment measured, under which harness, against what it was running

Revision ID: a1d6e83f2b47
Revises: f9c5a7b3e401
Create Date: 2026-08-20 20:40:00.000000

One table. A benchmark is carried out by an `Operation` — that machinery already
has phases, a deadline, cancellation and replay across a reconnect, and none of it
is worth building twice — so what is missing is only the record of the run and its
result, which is this.

`summary` is JSONB and holds the harness's own output verbatim. No normalised
metric columns: the union of AIPerf's result shape and vLLM's is a schema decision
that should be made after both have been read against real deployments, and a
premature one would be permanent. Same-harness comparison is what this table
promises, and `harness` plus `harness_version` are what make that promise hold
across a release that redefines a percentile.

The provenance columns — `model`, `deployment_generation`, `recipe_id`,
`recipe_version`, `engine_args` — are snapshots taken at creation rather than
joins. The recipe is mutable and the deployment is disposable; a run has to keep
describing what it measured after both have moved on.

`operation_id` is `SET NULL` rather than `CASCADE`. The operation is the errand and
the run is the record, and the record is the half worth keeping.

No backfill: nothing has been benchmarked yet.
"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a1d6e83f2b47"
down_revision: Union[str, None] = "f9c5a7b3e401"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ops_benchmark_run",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("deployment_id", sa.UUID(), nullable=False),
        sa.Column("cluster_id", sa.UUID(), nullable=False),
        sa.Column("operation_id", sa.UUID(), nullable=True),
        sa.Column("harness", sa.String(length=32), nullable=False),
        sa.Column("harness_version", sa.String(length=64), nullable=True),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("deployment_generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("recipe_id", sa.UUID(), nullable=True),
        sa.Column("recipe_version", sa.Integer(), nullable=True),
        sa.Column("engine_args", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("phase", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("artifacts_uri", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["cluster_id"], ["supply_cluster.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deployment_id"], ["demand_deployment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["operation_id"], ["ops_operation.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recipe_id"], ["demand_recipe.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
    )
    op.create_index("ix_ops_benchmark_run_cluster_id", "ops_benchmark_run", ["cluster_id"])
    op.create_index("ix_ops_benchmark_run_deployment_id", "ops_benchmark_run", ["deployment_id"])
    op.create_index("ix_ops_benchmark_run_deployment_seq", "ops_benchmark_run", ["deployment_id", "seq"])
    op.create_index("ix_ops_benchmark_run_harness", "ops_benchmark_run", ["harness"])
    op.create_index("ix_ops_benchmark_run_operation_id", "ops_benchmark_run", ["operation_id"])
    op.create_index("ix_ops_benchmark_run_phase", "ops_benchmark_run", ["phase"])
    op.create_index("ix_ops_benchmark_run_recipe_id", "ops_benchmark_run", ["recipe_id"])


def downgrade() -> None:
    op.drop_table("ops_benchmark_run")
