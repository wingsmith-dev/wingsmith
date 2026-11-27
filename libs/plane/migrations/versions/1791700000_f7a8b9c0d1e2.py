"""demand_media_job table and bind_project.media_retention_days (ADR 0041)

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-11-27 13:19:09.170303

"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "demand_media_job",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False, start=1000), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("endpoint_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("path", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("object_key", sa.String(length=512), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("upstream_seconds", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("units", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_media_job_status"
        ),
        sa.ForeignKeyConstraint(["endpoint_id"], ["demand_endpoint.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["bind_project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["bind_tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_media_job_idempotency"),
        sa.UniqueConstraint("uid"),
    )
    op.create_index("ix_demand_media_job_claim", "demand_media_job", ["status", "lease_expires_at"], unique=False)
    op.create_index(op.f("ix_demand_media_job_project_id"), "demand_media_job", ["project_id"], unique=False)
    op.create_index("ix_demand_media_job_retention", "demand_media_job", ["retention_until"], unique=False)
    op.create_index(op.f("ix_demand_media_job_tenant_id"), "demand_media_job", ["tenant_id"], unique=False)
    op.add_column("bind_project", sa.Column("media_retention_days", sa.Integer(), server_default="7", nullable=False))


def downgrade() -> None:
    op.drop_column("bind_project", "media_retention_days")
    op.drop_index(op.f("ix_demand_media_job_tenant_id"), table_name="demand_media_job")
    op.drop_index("ix_demand_media_job_retention", table_name="demand_media_job")
    op.drop_index(op.f("ix_demand_media_job_project_id"), table_name="demand_media_job")
    op.drop_index("ix_demand_media_job_claim", table_name="demand_media_job")
    op.drop_table("demand_media_job")
