"""cluster metrics label

Revision ID: b719e4e78b13
Revises: fac33e4dd051
Create Date: 2026-08-27 19:03:52.990111

Existing rows are left null, not backfilled with their own name. Null
means "not observable": metrics report no source for the cluster instead of
querying the shared store under a label nothing has confirmed, where an empty
answer would read as a healthy idle fleet.

Opting a cluster in is one statement, once the store's `cluster` label shows it:

    UPDATE supply_cluster SET metrics_label = name WHERE name = '<cluster>';

or, over the API, `PATCH /v1/clusters/<ref> {"metrics_label": "<label>"}`.
"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'b719e4e78b13'
down_revision: Union[str, None] = 'fac33e4dd051'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('supply_cluster', sa.Column('metrics_label', sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column('supply_cluster', 'metrics_label')
