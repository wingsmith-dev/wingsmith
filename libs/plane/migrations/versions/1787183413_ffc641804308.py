"""discovered workloads and the decision to track them

Revision ID: ffc641804308
Revises: 07ccbf04c05d
Create Date: 2026-08-19 20:50:13.403246

Two tables because they have two writers. `demand_discovered_workload` is a sighting — what
the operator found already running when it arrived — and ingestion is its only writer.
`demand_workload_adoption` is what a human decided about that sighting, written only by the
API, keyed by the cluster's stable workload uid rather than a foreign key so a decision
survives the row being pruned and rediscovered.

Neither table feeds `deployment.desired`. Adopting a running workload attaches a name and a
project to it and nothing else; declaring what is already running is how a control plane
restarts it, and reloading weights into VRAM is the cost this split exists to avoid.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'ffc641804308'
down_revision: Union[str, None] = '07ccbf04c05d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('demand_discovered_workload',
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('seq', sa.Integer(), sa.Identity(always=False, start=1000, cycle=True), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('uid', sa.String(length=40), nullable=False),
    sa.Column('cluster_id', sa.UUID(), nullable=False),
    sa.Column('namespace', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=255), nullable=True),
    sa.Column('images', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('replicas_desired', sa.Integer(), server_default='0', nullable=False),
    sa.Column('replicas_ready', sa.Integer(), server_default='0', nullable=False),
    sa.Column('devices_per_replica', sa.Integer(), server_default='0', nullable=False),
    sa.Column('devices_total', sa.Integer(), server_default='0', nullable=False),
    sa.Column('node_uids', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('labels', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['cluster_id'], ['supply_cluster.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('cluster_id', 'namespace', 'name', 'kind', name='uq_demand_discovered_workload_identity'),
    sa.UniqueConstraint('uid')
    )
    op.create_index(op.f('ix_demand_discovered_workload_cluster_id'), 'demand_discovered_workload', ['cluster_id'], unique=False)
    op.create_table('demand_workload_adoption',
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('seq', sa.Integer(), sa.Identity(always=False, start=1000, cycle=True), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('cluster_id', sa.UUID(), nullable=False),
    sa.Column('workload_uid', sa.String(length=40), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('decided_by', sa.String(length=255), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['cluster_id'], ['supply_cluster.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['bind_project.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('cluster_id', 'workload_uid', name='uq_demand_workload_adoption_identity')
    )
    op.create_index(op.f('ix_demand_workload_adoption_cluster_id'), 'demand_workload_adoption', ['cluster_id'], unique=False)
    op.create_index(op.f('ix_demand_workload_adoption_decision'), 'demand_workload_adoption', ['decision'], unique=False)
    op.create_index(op.f('ix_demand_workload_adoption_project_id'), 'demand_workload_adoption', ['project_id'], unique=False)
    op.create_index(op.f('ix_demand_workload_adoption_workload_uid'), 'demand_workload_adoption', ['workload_uid'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_demand_workload_adoption_workload_uid'), table_name='demand_workload_adoption')
    op.drop_index(op.f('ix_demand_workload_adoption_project_id'), table_name='demand_workload_adoption')
    op.drop_index(op.f('ix_demand_workload_adoption_decision'), table_name='demand_workload_adoption')
    op.drop_index(op.f('ix_demand_workload_adoption_cluster_id'), table_name='demand_workload_adoption')
    op.drop_table('demand_workload_adoption')
    op.drop_index(op.f('ix_demand_discovered_workload_cluster_id'), table_name='demand_discovered_workload')
    op.drop_table('demand_discovered_workload')
