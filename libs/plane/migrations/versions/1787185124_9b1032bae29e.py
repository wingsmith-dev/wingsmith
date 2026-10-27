"""pin an adopted deployment to the workload it was promoted from

Revision ID: 9b1032bae29e
Revises: ffc641804308
Create Date: 2026-08-19 21:18:44.752486

The digest is the gate. `spec_fingerprint` is what the cluster says a workload's pod
template currently is; `adopted_fingerprint` is what it was when we accepted the
promotion. A reconciler that finds the two disagreeing refuses and reports instead of
converging, because converging means replacing a serving pod template and reloading every model
behind it, which is the cost the whole adoption path exists to avoid.

Both are nullable. A deployment created the ordinary way was never adopted and has no
digest to be measured against, and a sighting from an operator too old to publish one is
still worth listing: it simply cannot be promoted.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = '9b1032bae29e'
down_revision: Union[str, None] = 'ffc641804308'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('demand_deployment', sa.Column('adopted_workload_uid', sa.String(length=40), nullable=True))
    op.add_column('demand_deployment', sa.Column('adopted_fingerprint', sa.String(length=80), nullable=True))
    op.create_index(op.f('ix_demand_deployment_adopted_workload_uid'), 'demand_deployment', ['adopted_workload_uid'], unique=False)
    op.add_column('demand_discovered_workload', sa.Column('spec_fingerprint', sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column('demand_discovered_workload', 'spec_fingerprint')
    op.drop_index(op.f('ix_demand_deployment_adopted_workload_uid'), table_name='demand_deployment')
    op.drop_column('demand_deployment', 'adopted_fingerprint')
    op.drop_column('demand_deployment', 'adopted_workload_uid')
