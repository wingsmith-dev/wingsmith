"""drop the non-unique indexes that duplicate a unique constraint

Revision ID: 03684fe272a5
Revises: f3c58b7a20d9
Create Date: 2026-10-15 10:00:00.000000

Four columns were created with a unique constraint and a separate plain index on the
same column. The constraint's own index already serves every lookup, so the second
index only cost writes, and the models declared a unique index instead of the pair, so
`alembic check` reported drift on every run. The models now declare `unique=True` alone,
which is the constraint this schema already has.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "03684fe272a5"
down_revision: str | None = "f3c58b7a20d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


REDUNDANT = (
    ("ix_bind_serving_quota_endpoint_id", "bind_serving_quota", "endpoint_id"),
    ("ix_bind_tenant_api_key_key_hash", "bind_tenant_api_key", "key_hash"),
    ("ix_bind_tenant_token_token_hash", "bind_tenant_token", "token_hash"),
    ("ix_demand_endpoint_alias_alias", "demand_endpoint_alias", "alias"),
)


def upgrade() -> None:
    for name, table, _column in REDUNDANT:
        op.drop_index(name, table_name=table)


def downgrade() -> None:
    for name, table, column in REDUNDANT:
        op.create_index(name, table, [column], unique=False)
