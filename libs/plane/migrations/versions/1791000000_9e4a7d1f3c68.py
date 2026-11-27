"""cluster kind: kubernetes or host

Revision ID: 9e4a7d1f3c68
Revises: 8d3f6c0e2b57
Create Date: 2026-11-27 18:00:00.000000

A host (one bare Linux machine running wingsmith-host) is a cluster with kind=host and exactly
one node. Declared at registration; every existing cluster is Kubernetes.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "9e4a7d1f3c68"
down_revision: str | None = "8d3f6c0e2b57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "supply_cluster",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="kubernetes"),
    )
    op.create_check_constraint("ck_supply_cluster_kind", "supply_cluster", "kind IN ('kubernetes', 'host')")


def downgrade() -> None:
    op.drop_constraint("ck_supply_cluster_kind", "supply_cluster", type_="check")
    op.drop_column("supply_cluster", "kind")
