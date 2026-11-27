"""media unit limits on serving quotas

Revision ID: d8e9f0a1b2c3
Revises: c4d5e6f7a8b9
Create Date: 2026-11-27 23:00:00.000000

P4.2: an image, video or audio endpoint sells units per minute (images by size tier, video
seconds, speech characters and seconds, transcription seconds) beside the request and token rates.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bind_serving_quota",
        sa.Column("unit_limits", postgresql.JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("bind_serving_quota", "unit_limits")
