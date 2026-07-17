"""a workload keeps its serving configurations, and a replica says which one it came up on

Revision ID: c7f2a9d41e86
Revises: e4b7a2c91d53
Create Date: 2026-09-12 09:00:00.000000

Additive on both halves, and neither re-renders anything. `demand_workload_revision` is a new
table nothing reads yet; `demand_replica.serving_digest` is a nullable column carrying a value
the operator already puts on the wire — `ReplicaObserved.serving_digest` has been sent and
dropped since disaggregation. No declared column moves, no `serving_digest` is recomputed, and
no deployment's generation is touched, so no cluster sees a change of any kind.

**No backfill, on purpose.** A revision's `snapshot` is a serving patch body, and building
one here would mean a stub standing in for `Workload` and `Artifact` across some forty
attributes. `a7c31f8e64b2` already paid for that lesson twice and wrote it down: a stub that is
a subset of what the live function reads is a migration that crashes on a fresh database the day
that function grows a field. So the history starts empty, and a workload older than this table
records its existing configuration as an `imported` revision from the pre-image its next serving
edit reads — which is code that is covered by tests and moves with the schema, rather than a
snapshot frozen at the shape this file happened to know.

Replicas already running keep `serving_digest` null until their cluster reports them again,
which every agent does on its next resync. Null there means "not known", never "the declared
one".
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "c7f2a9d41e86"
down_revision: str | None = "e4b7a2c91d53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "demand_workload_revision",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(always=False, start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("workload_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("serving_digest", sa.String(length=71), nullable=False),
        sa.Column("artifact_uid", sa.String(length=40), server_default="", nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("reason", sa.String(length=32), server_default="updated", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["workload_id"], ["demand_workload.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
        sa.UniqueConstraint("workload_id", "number", name="uq_demand_workload_revision_number"),
    )
    op.create_index(
        "ix_demand_workload_revision_workload_id", "demand_workload_revision", ["workload_id"], unique=False
    )
    op.create_index(
        "ix_demand_workload_revision_serving_digest", "demand_workload_revision", ["serving_digest"], unique=False
    )
    op.add_column("demand_replica", sa.Column("serving_digest", sa.String(length=71), nullable=True))
    op.create_index("ix_demand_replica_serving_digest", "demand_replica", ["serving_digest"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_demand_replica_serving_digest", table_name="demand_replica")
    op.drop_column("demand_replica", "serving_digest")
    op.drop_index("ix_demand_workload_revision_serving_digest", table_name="demand_workload_revision")
    op.drop_index("ix_demand_workload_revision_workload_id", table_name="demand_workload_revision")
    op.drop_table("demand_workload_revision")
