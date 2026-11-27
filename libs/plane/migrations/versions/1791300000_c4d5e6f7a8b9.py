"""field manager on workloads and endpoints

Revision ID: c4d5e6f7a8b9
Revises: b7e1d2c3f4a5
Create Date: 2026-11-27 22:00:00.000000

Who applied an object last (ADR 0033): the field manager (`?fieldManager=`), the principal behind
it and when. An object already applied was applied by `wings apply`, the only manager so far.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b7e1d2c3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("demand_workload", "demand_endpoint")

BACKFILL = "UPDATE {table} SET applied_by = 'wings-cli' WHERE last_applied IS NOT NULL AND applied_by IS NULL"


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("applied_by", sa.String(128), nullable=True))
        op.add_column(table, sa.Column("applied_by_principal", sa.String(255), nullable=True))
        op.add_column(table, sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(BACKFILL.format(table=table))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "applied_at")
        op.drop_column(table, "applied_by_principal")
        op.drop_column(table, "applied_by")
