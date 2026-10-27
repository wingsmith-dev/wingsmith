"""a tenant can sign in to its own serving view

Revision ID: c2e8a41f7b06
Revises: a4f5b1c72d99
Create Date: 2026-09-07 12:00:00.000000

Until now the plane had exactly one human credential: a shared admin password that resolves to
a principal holding `*`. That is the right shape for the people who operate the fleet and the
wrong shape for the people we serve, who need to see one tenant's endpoints and must not be able
to see a cluster.

`bind_tenant_token` is that second credential. It is a console credential and never an inference
key: the gateway authenticates the system in front of it, and a token that named a tenant there
would make that hop a credential store. The refresh token gains the credential it was opened
with, so revoking a tenant's token ends the sessions it opened instead of only refusing the
next sign-in.
"""

from typing import Union, Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c2e8a41f7b06"
down_revision: Union[str, None] = "a4f5b1c72d99"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bind_tenant_token",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["bind_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_bind_tenant_token_tenant_id", "bind_tenant_token", ["tenant_id"])
    op.create_index("ix_bind_tenant_token_token_hash", "bind_tenant_token", ["token_hash"])

    op.add_column("auth_refresh_token", sa.Column("tenant_token_id", sa.UUID(), nullable=True))
    op.create_index("ix_auth_refresh_token_tenant_token_id", "auth_refresh_token", ["tenant_token_id"])
    op.create_foreign_key(
        "fk_auth_refresh_token_tenant_token",
        "auth_refresh_token",
        "bind_tenant_token",
        ["tenant_token_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_auth_refresh_token_tenant_token", "auth_refresh_token", type_="foreignkey")
    op.drop_index("ix_auth_refresh_token_tenant_token_id", table_name="auth_refresh_token")
    op.drop_column("auth_refresh_token", "tenant_token_id")
    op.drop_index("ix_bind_tenant_token_token_hash", table_name="bind_tenant_token")
    op.drop_index("ix_bind_tenant_token_tenant_id", table_name="bind_tenant_token")
    op.drop_table("bind_tenant_token")
