"""supply_cluster_project.namespaces: the namespaces allowed to declare into a linked project (ADR 0042)

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-11-27 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "b9c0d1e2f3a4"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing links get an empty list: nothing allowed until namespaces are named.
    op.add_column(
        "supply_cluster_project",
        sa.Column("namespaces", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("supply_cluster_project", "namespaces")
