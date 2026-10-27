"""an adopted deployment carries the name of the object it adopted

Revision ID: c41d8a6b7e02
Revises: e5b2c73a9d18
Create Date: 2026-08-24 20:30:00.000000

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'c41d8a6b7e02'
down_revision: Union[str, None] = 'e5b2c73a9d18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('demand_deployment', sa.Column('adopted_namespace', sa.String(length=253), nullable=True))
    op.add_column('demand_deployment', sa.Column('adopted_kind', sa.String(length=32), nullable=True))
    op.add_column('demand_deployment', sa.Column('adopted_name', sa.String(length=253), nullable=True))

    # Backfilled from the sighting the wire used to be rendered from, which is the only place the
    # namespace has ever been recorded. A deployment whose sighting is pruned at this instant gets
    # nothing and keeps rendering no `adopt` block until it is promoted again: the same state it
    # was already in, and not one this migration can invent its way out of.
    op.execute(
        sa.text(
            """
            UPDATE demand_deployment d
               SET adopted_namespace = w.namespace,
                   adopted_kind = w.kind,
                   adopted_name = w.name
              FROM demand_discovered_workload w
             WHERE w.cluster_id = d.cluster_id
               AND w.uid = d.adopted_workload_uid
               AND d.adopted_workload_uid IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_column('demand_deployment', 'adopted_name')
    op.drop_column('demand_deployment', 'adopted_kind')
    op.drop_column('demand_deployment', 'adopted_namespace')
