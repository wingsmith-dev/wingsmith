"""cluster metrics label

Revision ID: b719e4e78b13
Revises: fac33e4dd051
Create Date: 2026-08-27 19:03:52.990111

Existing rows are left null rather than backfilled with their own name. Null
means "not observable" and keeps today's honest "no metrics source" answer; a
name would point every cluster at the shared store under a label nothing has
confirmed, and a store holding no such label answers a clean empty that reads as
a healthy idle fleet. `cluster-us` and `cluster-ap` are the
rows this protects: their series stopped landing and they were delisted from
`HANGAR_METRICS_SOURCES` on purpose, and a blanket backfill would quietly undo
that.

The one cluster that value does list is carried over by name, because the
label-to-cluster mapping is moving off the ConfigMap and onto the row and this is
the row it moves onto. Nothing that works today stops working, and nothing that
reports no source starts reporting an empty chart.

Opting a further cluster in is one statement, after the store's `cluster` label
shows it:

    UPDATE supply_cluster SET metrics_label = name WHERE name = '<cluster>';

or, over the API, `PATCH /v1/clusters/<ref> {"metrics_label": "<label>"}`.

Autogenerate also proposed reshaping `bind_serving_quota`'s unique constraint
into a unique index. That is pre-existing drift with nothing to do with this
change and is left alone.
"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'b719e4e78b13'
down_revision: Union[str, None] = 'fac33e4dd051'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ENROLLED = 'cluster-a'
"""The one cluster the deployed HANGAR_METRICS_SOURCES listed when this landed."""


def upgrade() -> None:
    op.add_column('supply_cluster', sa.Column('metrics_label', sa.String(length=128), nullable=True))
    op.execute(
        sa.text('UPDATE supply_cluster SET metrics_label = name WHERE name = :name').bindparams(name=ENROLLED)
    )


def downgrade() -> None:
    op.drop_column('supply_cluster', 'metrics_label')
