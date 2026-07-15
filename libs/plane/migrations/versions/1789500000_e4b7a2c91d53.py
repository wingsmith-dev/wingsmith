"""a workload declares its frontend router settings

Revision ID: e4b7a2c91d53
Revises: d9a1c604e738
Create Date: 2026-09-11 10:00:00.000000

Additive. `demand_workload.router` holds only the settings a workload declares, and `{}` is every
existing row, which is also what "declared nothing" is stored as from now on. `serving_digest`
leaves the block out when it is empty, so no stored digest changes and no deployment re-renders
because this column exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "e4b7a2c91d53"
down_revision: str | None = "d9a1c604e738"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "demand_workload",
        sa.Column("router", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("demand_workload", "router")
