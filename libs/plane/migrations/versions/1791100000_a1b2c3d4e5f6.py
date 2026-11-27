"""workload runtime kind and spec; cluster runtimes

Revision ID: a1b2c3d4e5f6
Revises: 9e4a7d1f3c68
Create Date: 2026-11-27 20:00:00.000000

P8.2: a workload runs under `dynamo` (every existing row) or a custom `container`. The cluster's
sync state records which runtimes its operator discovered; null keeps today's reading.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "9e4a7d1f3c68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "demand_workload",
        sa.Column("runtime_kind", sa.String(length=16), nullable=False, server_default="dynamo"),
    )
    op.add_column(
        "demand_workload",
        sa.Column("runtime_spec", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_check_constraint(
        "ck_demand_workload_runtime_kind", "demand_workload", "runtime_kind IN ('dynamo', 'container')"
    )
    op.add_column(
        "supply_cluster_sync_state",
        sa.Column("runtimes", postgresql.ARRAY(sa.String(length=16)), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("supply_cluster_sync_state", "runtimes")
    op.drop_constraint("ck_demand_workload_runtime_kind", "demand_workload", type_="check")
    op.drop_column("demand_workload", "runtime_spec")
    op.drop_column("demand_workload", "runtime_kind")
