"""benchmark runs record the image they ran, and may be given one per run

Revision ID: b2e7f94c3d58
Revises: a1d6e83f2b47
Create Date: 2026-08-20 21:10:00.000000

The harness image used to live only in the plane's harness table, which meant a
run could not say afterwards what produced its numbers: the default is a pin, the
pin moves between releases of this plane, and a row that recorded only `aiperf`
would silently span two tools. It is a provenance column like `engine_args`, and
it is written at creation from the harness default or from a per-run override.

Existing rows are backfilled with `nvcr.io/nvidia/ai-dynamo/aiperf:latest`, which
is what they were actually dispatched with — the column says what ran, so
backfilling it with the current default would be a lie about older runs. The
server default is empty rather than a real reference for the same reason: a row
that somehow arrives without one should read as unknown, not as a claim.
"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = "b2e7f94c3d58"
down_revision: Union[str, None] = "a1d6e83f2b47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ops_benchmark_run",
        sa.Column("image", sa.String(length=512), server_default="", nullable=False),
    )
    op.execute(
        "UPDATE ops_benchmark_run "
        "SET image = 'nvcr.io/nvidia/ai-dynamo/aiperf:latest' "
        "WHERE image = '' AND harness = 'aiperf'"
    )


def downgrade() -> None:
    op.drop_column("ops_benchmark_run", "image")
