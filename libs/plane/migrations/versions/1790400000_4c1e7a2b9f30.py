"""a new tenant row asks for an API key unless it says otherwise

Revision ID: 4c1e7a2b9f30
Revises: 9d05d968be9c
Create Date: 2026-10-27 09:00:00.000000

`bind_tenant.auth` defaulted to `none` in the database while the create request
defaults to `api_key`, so a row written without the API (a seed script, a
hand-run insert) came up with no key check at the gateway. The column default
now matches the request. Existing rows keep the value they hold.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "4c1e7a2b9f30"
down_revision: str | None = "9d05d968be9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("bind_tenant", "auth", server_default="api_key")


def downgrade() -> None:
    op.alter_column("bind_tenant", "auth", server_default="none")
