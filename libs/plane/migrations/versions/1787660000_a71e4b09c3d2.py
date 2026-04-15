"""a cluster carries a data-plane serving address

Revision ID: a71e4b09c3d2
Revises: c41d8a6b7e02
Create Date: 2026-08-24 21:00:00.000000

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = "a71e4b09c3d2"
down_revision: Union[str, None] = "c41d8a6b7e02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "supply_cluster_serving",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("cluster_id", sa.UUID(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("credential_id", sa.UUID(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["supply_cluster.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["credential_id"], ["ops_credential.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cluster_id"),
    )


def downgrade() -> None:
    op.drop_table("supply_cluster_serving")
