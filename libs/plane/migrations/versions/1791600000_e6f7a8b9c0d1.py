"""change_log.created_at from clock_timestamp()

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-11-27 23:30:00.000000

`now()` is the transaction start, so a long transaction logged a fresh seq with an old
`created_at` and compaction could move past seconds-old rows (ADR 0038).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("change_log", "created_at", server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    op.alter_column("change_log", "created_at", server_default=sa.func.now())
