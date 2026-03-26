"""give an event a public identifier like every other resource

Revision ID: e2b8d05c4f16
Revises: d1a7c94b3e05
Create Date: 2026-08-20 00:20:00.000000

`Event` was the one resource addressed by its row primary key, so `GET /v1/events/{ref}`
took a bare UUID while every other path segment took a prefix. That is a rule with one
exception, which is two rules.

Existing rows are backfilled from `md5(id)`, whose hex digits are already the first
sixteen characters of the Crockford alphabet the minter uses — so a backfilled id passes
the same shape check a minted one does. It is not time-sortable the way a minted ULID is,
and it does not need to be: events page on `seq`, never on `uid`.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'e2b8d05c4f16'
down_revision: Union[str, None] = 'd1a7c94b3e05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ops_event', sa.Column('uid', sa.String(length=40), nullable=True))
    op.execute("UPDATE ops_event SET uid = 'evt_' || substr(md5(id::text), 1, 26)")
    op.alter_column('ops_event', 'uid', nullable=False)
    op.create_unique_constraint('uq_ops_event_uid', 'ops_event', ['uid'])


def downgrade() -> None:
    op.drop_constraint('uq_ops_event_uid', 'ops_event', type_='unique')
    op.drop_column('ops_event', 'uid')
