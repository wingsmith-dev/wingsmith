"""workloads and endpoints carry a spec generation

Revision ID: 5a8c3e1f7b24
Revises: c41f2a7d8e63
Create Date: 2026-11-26 15:00:00.000000

`generation` counts changes to what was declared on a workload or endpoint, and only spec writes
move it. It is what `revision` and `If-Match` now read, so observation (ingestion, status rollups)
no longer looks like a concurrent edit. Existing rows start at 1.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "5a8c3e1f7b24"
down_revision: str | None = "c41f2a7d8e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("demand_workload", "demand_endpoint"):
        op.add_column(table, sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"))


def downgrade() -> None:
    for table in ("demand_workload", "demand_endpoint"):
        op.drop_column(table, "generation")
