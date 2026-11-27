"""supply_cluster_project: projects a cluster may declare workloads into (ADR 0042)

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-11-27 15:00:00.000000

"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "supply_cluster_project",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False, start=1000), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("cluster_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["supply_cluster.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["bind_project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cluster_id", "project_id", name="uq_supply_cluster_project"),
    )
    op.create_index(
        op.f("ix_supply_cluster_project_cluster_id"), "supply_cluster_project", ["cluster_id"], unique=False
    )
    op.create_index(
        op.f("ix_supply_cluster_project_project_id"), "supply_cluster_project", ["project_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_supply_cluster_project_project_id"), table_name="supply_cluster_project")
    op.drop_index(op.f("ix_supply_cluster_project_cluster_id"), table_name="supply_cluster_project")
    op.drop_table("supply_cluster_project")
