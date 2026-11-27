"""endpoint targets can name a workload

Revision ID: 7c2e5b9d1a46
Revises: 6b9d4f2a8c35
Create Date: 2026-11-27 15:00:00.000000

A target is a deployment or a workload by name (ADR 0026). `deployment_id` becomes nullable and
exactly one of the two is set. Existing rows all carry a deployment, so nothing is rewritten.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "7c2e5b9d1a46"
down_revision: str | None = "6b9d4f2a8c35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("demand_endpoint_target", sa.Column("workload_name", sa.String(128), nullable=True))
    op.alter_column("demand_endpoint_target", "deployment_id", nullable=True)
    op.create_unique_constraint(
        "uq_demand_endpoint_target_workload", "demand_endpoint_target", ["endpoint_id", "workload_name"]
    )
    op.create_check_constraint(
        "ck_demand_endpoint_target_one_ref",
        "demand_endpoint_target",
        "(deployment_id IS NULL) <> (workload_name IS NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM demand_endpoint_target WHERE workload_name IS NOT NULL")
    op.drop_constraint("ck_demand_endpoint_target_one_ref", "demand_endpoint_target", type_="check")
    op.drop_constraint("uq_demand_endpoint_target_workload", "demand_endpoint_target", type_="unique")
    op.alter_column("demand_endpoint_target", "deployment_id", nullable=False)
    op.drop_column("demand_endpoint_target", "workload_name")
