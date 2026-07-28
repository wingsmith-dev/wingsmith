"""an endpoint carries the public names a caller may type, beside the one it is keyed on

Revision ID: f3c58b7a20d9
Revises: ead112828e54
Create Date: 2026-09-15 10:00:00.000000

`demand_endpoint_alias` is new: one row per additional model id that resolves to an endpoint.
`alias` is globally unique, which is what makes aliases and canonical slugs one namespace as far
as a constraint can reach — two endpoints cannot claim one alias. The rest of that namespace
spans two tables and is the service's to refuse.

Additive and empty. Nothing resolves differently until somebody writes a row, because the
resolved routing table carries an empty alias set for every endpoint that has none.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "f3c58b7a20d9"
down_revision: str | None = "ead112828e54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "demand_endpoint_alias",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(always=False, start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["endpoint_id"], ["demand_endpoint.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alias"),
    )
    op.create_index("ix_demand_endpoint_alias_endpoint_id", "demand_endpoint_alias", ["endpoint_id"], unique=False)
    op.create_index("ix_demand_endpoint_alias_alias", "demand_endpoint_alias", ["alias"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_demand_endpoint_alias_alias", table_name="demand_endpoint_alias")
    op.drop_index("ix_demand_endpoint_alias_endpoint_id", table_name="demand_endpoint_alias")
    op.drop_table("demand_endpoint_alias")
