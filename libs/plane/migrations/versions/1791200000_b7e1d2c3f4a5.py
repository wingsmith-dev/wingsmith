"""last-applied manifest on workloads and endpoints

Revision ID: b7e1d2c3f4a5
Revises: a1b2c3d4e5f6
Create Date: 2026-11-27 20:00:00.000000

The body the last `wings apply` sent, which the next PUT three-way merges against (ADR 0032).
Null until an object is first applied.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "b7e1d2c3f4a5"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("demand_workload", sa.Column("last_applied", JSONB(), nullable=True))
    op.add_column("demand_endpoint", sa.Column("last_applied", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("demand_endpoint", "last_applied")
    op.drop_column("demand_workload", "last_applied")
