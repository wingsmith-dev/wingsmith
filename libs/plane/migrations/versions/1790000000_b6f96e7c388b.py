"""every seq is a 64-bit identity that does not wrap

Revision ID: b6f96e7c388b
Revises: 03684fe272a5
Create Date: 2026-10-15 10:10:00.000000

`seq` is the ordering and pagination key on every table, and the reactor's cursor into
`ops_event`. It was a 32-bit identity with `CYCLE`, so after about 2.1 billion inserts
on a table it would restart at the minimum and every cursor past that point would skip
or repeat rows. It becomes `bigint` with `NO CYCLE`: exhausting it is an insert error,
never a silent wrap.

Rewrites every table that has the column, under an exclusive lock per table, so expect
the upgrade to take as long as a full copy of the larger tables (`ops_event` first among
them). The identity's sequence follows the column to `bigint`
and takes that type's maximum.

The downgrade narrows the column back and fails if any value no longer fits in 32 bits.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "b6f96e7c388b"
down_revision: str | None = "03684fe272a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLES = """
    SELECT table_name
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND column_name = 'seq'
       AND is_identity = 'YES'
     ORDER BY table_name
"""


def _tables() -> list[str]:
    return list(op.get_bind().execute(sa.text(TABLES)).scalars())


def upgrade() -> None:
    for table in _tables():
        op.execute(f'ALTER TABLE "{table}" ALTER COLUMN seq TYPE bigint, ALTER COLUMN seq SET NO CYCLE')


def downgrade() -> None:
    for table in _tables():
        op.execute(f'ALTER TABLE "{table}" ALTER COLUMN seq TYPE integer, ALTER COLUMN seq SET CYCLE')
