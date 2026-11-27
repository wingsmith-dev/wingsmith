"""change log and resource_version

Revision ID: d5e6f7a8b9c0
Revises: d8e9f0a1b2c3
Create Date: 2026-11-27 23:00:00.000000

The durable, ordered change log for workloads, endpoints and artifacts (ADR 0037), the row that
records how far it has been compacted, and `resource_version` on the three tables. Existing rows
start at 0: no change has been logged for them yet, and the first write moves them onto the log.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("demand_workload", "demand_endpoint", "demand_artifact")


def upgrade() -> None:
    op.create_table(
        "change_log",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("uid", sa.String(40), nullable=False),
        sa.Column("op", sa.String(16), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_change_log_created_at", "change_log", ["created_at"])
    op.create_table(
        "change_log_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("compacted_through", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.execute("INSERT INTO change_log_state (id, compacted_through) VALUES (1, 0)")
    for table in TABLES:
        op.add_column(table, sa.Column("resource_version", sa.BigInteger(), server_default="0", nullable=False))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "resource_version")
    op.drop_table("change_log_state")
    op.drop_index("ix_change_log_created_at", table_name="change_log")
    op.drop_table("change_log")
