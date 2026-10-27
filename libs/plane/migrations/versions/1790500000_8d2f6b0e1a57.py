"""operations, benchmark runs, events and alerts outlive the cluster they were about

Revision ID: 8d2f6b0e1a57
Revises: 4c1e7a2b9f30
Create Date: 2026-10-27 09:10:00.000000

Deleting a drained cluster deleted every operation, benchmark run, event and alert that
named it, because each `cluster_id` foreign key cascaded. That is the history somebody
reads to find out why the cluster was drained. The four references become
`ON DELETE SET NULL`; every one of the columns is already nullable.

The existing constraint names differ between tables, so each is read from `pg_constraint`.
The new ones follow the `fk_<table>_cluster_id` pattern.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "8d2f6b0e1a57"
down_revision: str | None = "4c1e7a2b9f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLES = ("ops_operation", "ops_benchmark_run", "ops_event", "ops_alert")


def _repoint(table: str, ondelete: str) -> None:
    name = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT c.conname FROM pg_constraint c JOIN pg_attribute a"
                " ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)"
                " WHERE c.contype = 'f' AND c.conrelid = CAST(:table AS regclass)"
                " AND c.confrelid = CAST('supply_cluster' AS regclass) AND a.attname = 'cluster_id'"
            ),
            {"table": table},
        )
        .scalar_one()
    )
    op.drop_constraint(name, table, type_="foreignkey")
    op.create_foreign_key(f"fk_{table}_cluster_id", table, "supply_cluster", ["cluster_id"], ["id"], ondelete=ondelete)


def upgrade() -> None:
    for table in TABLES:
        _repoint(table, "SET NULL")


def downgrade() -> None:
    for table in TABLES:
        _repoint(table, "CASCADE")
