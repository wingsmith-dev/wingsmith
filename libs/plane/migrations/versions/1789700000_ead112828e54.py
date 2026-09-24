"""a tenant carries the settings the gateway will trust it by, and a table of inference keys

Revision ID: ead112828e54
Revises: c7f2a9d41e86
Create Date: 2026-09-12 10:00:00.000000

`bind_tenant` gains `ingress`, `auth` and `host`. `ingress` and `auth` default to `internal`
and `none`, which is how tenants behind an operator's own edge run, so every existing tenant reads
as it already behaves. `host` is nullable and unique, and empty for every tenant until one is
given a hostname to answer on.

`bind_tenant_api_key` is new: a gateway-side credential, deliberately its own table rather than
a column reused from `bind_tenant_token` — that one signs in to the console, this one calls the
gateway, and a single table would mean revoking one kind of credential could touch the other.

Additive, and nothing reads any of it yet: the resolved routing table does not carry `tenant_id`
until docs/plans/17's next step, so the gateway has no way to reach these columns from this
migration alone.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "ead112828e54"
down_revision: str | None = "c7f2a9d41e86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bind_tenant", sa.Column("ingress", sa.String(length=16), server_default="internal", nullable=False))
    op.add_column("bind_tenant", sa.Column("auth", sa.String(length=16), server_default="none", nullable=False))
    op.add_column("bind_tenant", sa.Column("host", sa.String(length=255), nullable=True))
    op.create_unique_constraint("uq_bind_tenant_host", "bind_tenant", ["host"])
    op.create_check_constraint("ck_bind_tenant_ingress", "bind_tenant", "ingress IN ('internal', 'public')")
    op.create_check_constraint("ck_bind_tenant_auth", "bind_tenant", "auth IN ('none', 'api_key')")

    op.create_table(
        "bind_tenant_api_key",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(always=False, start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["bind_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_bind_tenant_api_key_tenant_id", "bind_tenant_api_key", ["tenant_id"], unique=False)
    op.create_index("ix_bind_tenant_api_key_key_hash", "bind_tenant_api_key", ["key_hash"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_bind_tenant_api_key_key_hash", table_name="bind_tenant_api_key")
    op.drop_index("ix_bind_tenant_api_key_tenant_id", table_name="bind_tenant_api_key")
    op.drop_table("bind_tenant_api_key")

    op.drop_constraint("ck_bind_tenant_auth", "bind_tenant", type_="check")
    op.drop_constraint("ck_bind_tenant_ingress", "bind_tenant", type_="check")
    op.drop_constraint("uq_bind_tenant_host", "bind_tenant", type_="unique")
    op.drop_column("bind_tenant", "host")
    op.drop_column("bind_tenant", "auth")
    op.drop_column("bind_tenant", "ingress")
