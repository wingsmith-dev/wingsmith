"""make event replay idempotency a constraint instead of a guess

Revision ID: b5e1c38d7a49
Revises: a4d0f27b6c38
Create Date: 2026-08-20 00:50:00.000000

Ingestion de-duplicated events with a `NOT EXISTS` over cluster, type, subject kind,
instant and message. Two sessions replaying the same frame both read nothing and both
insert, which is exactly the case replay exists to survive.

Two details decide whether the index actually stops it. `cluster_id` is null for events
the plane raises about itself, and under the default rule two nulls never conflict — so a
plain unique index would exclude precisely the rows nothing else de-duplicates; `NULLS
NOT DISTINCT` is what makes null a value here, the same choice `uq_ops_cache_entry_scope`
made and for the same reason. And `message` is unbounded text, where a btree entry over
about 2.7kB is refused on insert — a long message would turn a duplicate event into a
frame that can never be acked. It is hashed, so the index entry is fixed width and the
conflict target is `md5(message)`.

Duplicates already in the table are collapsed to their earliest row first, since the
index cannot be built over them.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'b5e1c38d7a49'
down_revision: Union[str, None] = 'a4d0f27b6c38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM ops_event e USING ops_event keep "
        "WHERE e.seq > keep.seq "
        "AND e.cluster_id IS NOT DISTINCT FROM keep.cluster_id "
        "AND e.type = keep.type AND e.subject_type = keep.subject_type "
        "AND e.occurred_at = keep.occurred_at AND md5(e.message) = md5(keep.message)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ops_event_occurrence ON ops_event "
        "(cluster_id, type, subject_type, occurred_at, md5(message)) NULLS NOT DISTINCT"
    )


def downgrade() -> None:
    op.drop_index('uq_ops_event_occurrence', table_name='ops_event')
