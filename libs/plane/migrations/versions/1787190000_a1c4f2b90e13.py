"""give a deployment an observed half of its own

Revision ID: a1c4f2b90e13
Revises: 9b1032bae29e
Create Date: 2026-08-19 22:40:00.000000

`deployment.observed` arrives from the cluster and had nowhere to land: every column on
`demand_deployment` is declared state written by the API. Adding `phase` and the replica
counts there would put two writers on one row, which is the difference rule 1 exists to
keep. `ops_component_status` already sets the shape (a declared row and an observed row,
read together), and this is the same shape for deployments.

`observed_generation` is what makes the counts readable during a rollout: four ready at
the previous generation and four ready at the current one are the same four integers and
different facts.

`uq_ops_cache_entry_scope` becomes `NULLS NOT DISTINCT` in the same revision. `node_id` is
null for cluster-scoped residency, and under the default rule two nulls never conflict, so
the cluster-scoped row could never be upserted and every observation would append another
copy of the same fact.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'a1c4f2b90e13'
down_revision: Union[str, None] = '9b1032bae29e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('demand_deployment_status',
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('seq', sa.Integer(), sa.Identity(always=False, start=1000, cycle=True), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('deployment_id', sa.UUID(), nullable=False),
    sa.Column('cluster_id', sa.UUID(), nullable=False),
    sa.Column('observed_generation', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('phase', sa.String(length=32), server_default='pending', nullable=False),
    sa.Column('desired_replicas', sa.Integer(), server_default='0', nullable=False),
    sa.Column('ready_replicas', sa.Integer(), server_default='0', nullable=False),
    sa.Column('updated_replicas', sa.Integer(), server_default='0', nullable=False),
    sa.Column('unavailable_replicas', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error_code', sa.String(length=64), nullable=True),
    sa.Column('last_error_message', sa.Text(), nullable=True),
    sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['cluster_id'], ['supply_cluster.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['deployment_id'], ['demand_deployment.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('deployment_id')
    )
    op.create_index(op.f('ix_demand_deployment_status_cluster_id'), 'demand_deployment_status', ['cluster_id'], unique=False)
    op.create_index(op.f('ix_demand_deployment_status_phase'), 'demand_deployment_status', ['phase'], unique=False)


    op.drop_constraint('uq_ops_cache_entry_scope', 'ops_cache_entry', type_='unique')
    op.create_unique_constraint(
        'uq_ops_cache_entry_scope',
        'ops_cache_entry',
        ['cluster_id', 'node_id', 'artifact_id'],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_constraint('uq_ops_cache_entry_scope', 'ops_cache_entry', type_='unique')
    op.create_unique_constraint(
        'uq_ops_cache_entry_scope', 'ops_cache_entry', ['cluster_id', 'node_id', 'artifact_id']
    )
    op.drop_index(op.f('ix_demand_deployment_status_phase'), table_name='demand_deployment_status')
    op.drop_index(op.f('ix_demand_deployment_status_cluster_id'), table_name='demand_deployment_status')
    op.drop_table('demand_deployment_status')
