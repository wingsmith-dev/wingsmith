"""workload becomes the global umbrella over per-cluster deployments

Revision ID: e5b2c73a9d18
Revises: 8f4ccc237d91
Create Date: 2026-08-24 00:00:00.000000

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = 'e5b2c73a9d18'
down_revision: Union[str, None] = '8f4ccc237d91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    fanned_out = bind.execute(
        sa.text(
            "SELECT workload_id FROM demand_deployment GROUP BY workload_id HAVING count(*) > 1"
        )
    ).scalars().all()
    if fanned_out:
        raise RuntimeError(
            f"cannot migrate: {len(fanned_out)} workload(s) already have more than one deployment"
        )

    orphans = bind.execute(
        sa.text(
            "SELECT uid FROM demand_workload w "
            "WHERE NOT EXISTS (SELECT 1 FROM demand_deployment d WHERE d.workload_id = w.id)"
        )
    ).scalars().all()
    if orphans:
        print(f"dropping {len(orphans)} workload(s) with no deployment: {', '.join(orphans)}")
        bind.execute(
            sa.text(
                "DELETE FROM demand_workload w "
                "WHERE NOT EXISTS (SELECT 1 FROM demand_deployment d WHERE d.workload_id = w.id)"
            )
        )

    op.add_column('demand_workload', sa.Column('name', sa.String(length=128), nullable=True))
    op.add_column('demand_workload', sa.Column('recipe_id', sa.UUID(), nullable=True))
    op.add_column('demand_workload', sa.Column('min_scale', sa.Integer(), server_default='1', nullable=True))
    op.add_column('demand_workload', sa.Column('max_scale', sa.Integer(), server_default='1', nullable=True))
    op.add_column('demand_workload', sa.Column('desired_scale', sa.Integer(), server_default='1', nullable=True))
    op.add_column('demand_workload', sa.Column('strategy', sa.String(length=16), server_default='pack', nullable=True))
    op.add_column(
        'demand_workload',
        sa.Column('cluster_floors', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=True),
    )
    op.add_column('demand_workload', sa.Column('max_clusters', sa.Integer(), nullable=True))
    op.add_column('demand_workload', sa.Column('pinned_cluster_id', sa.UUID(), nullable=True))
    op.add_column('demand_workload', sa.Column('paused', sa.Boolean(), server_default='false', nullable=True))
    op.add_column(
        'demand_workload',
        sa.Column('adapter_artifact_ids', postgresql.ARRAY(sa.UUID()), server_default='{}', nullable=True),
    )
    op.add_column(
        'demand_workload',
        sa.Column('overrides', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=True),
    )
    op.add_column(
        'demand_workload',
        sa.Column('labels', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=True),
    )
    op.add_column('demand_workload', sa.Column('requires_single_topology_domain', sa.Boolean(), nullable=True))
    op.add_column('demand_workload', sa.Column('topology_domain_kind', sa.String(length=32), nullable=True))

    op.add_column(
        'demand_deployment',
        sa.Column('admission_phase', sa.String(length=16), server_default='pending', nullable=True),
    )
    op.add_column('demand_deployment', sa.Column('admission_reason', sa.Text(), nullable=True))
    op.add_column('demand_deployment', sa.Column('queued_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('demand_deployment', sa.Column('admitted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('demand_deployment', sa.Column('placed_at', sa.DateTime(timezone=True), nullable=True))

    op.add_column('bind_allocation', sa.Column('deployment_id', sa.UUID(), nullable=True))

    op.execute(
        """
        UPDATE demand_deployment d SET
            admission_phase = w.admission_phase,
            admission_reason = w.admission_reason,
            queued_at = w.queued_at,
            admitted_at = w.admitted_at,
            placed_at = w.placed_at
        FROM demand_workload w WHERE d.workload_id = w.id
        """
    )

    op.execute(
        """
        UPDATE demand_workload w SET
            name = d.name,
            recipe_id = d.recipe_id,
            adapter_artifact_ids = d.adapter_artifact_ids,
            overrides = d.overrides,
            labels = d.labels,
            requires_single_topology_domain = d.requires_single_topology_domain,
            topology_domain_kind = d.topology_domain_kind,
            min_scale = d.min_replicas,
            max_scale = d.max_replicas,
            desired_scale = d.desired_replicas,
            strategy = 'pack',
            cluster_floors = '{}'::jsonb,
            paused = false
        FROM demand_deployment d WHERE d.workload_id = w.id
        """
    )

    op.execute(
        """
        UPDATE demand_workload w SET name = w.name || '-' || ranked.rank::text
        FROM (
            SELECT id, row_number() OVER (
                PARTITION BY project_id, name ORDER BY created_at, id
            ) AS rank
            FROM demand_workload
        ) ranked
        WHERE ranked.id = w.id AND ranked.rank > 1
        """
    )

    op.execute(
        """
        UPDATE bind_allocation a SET deployment_id = d.id
        FROM demand_deployment d WHERE d.workload_id = a.workload_id
        """
    )

    for column in (
        'name',
        'recipe_id',
        'min_scale',
        'max_scale',
        'desired_scale',
        'strategy',
        'cluster_floors',
        'paused',
        'adapter_artifact_ids',
        'overrides',
        'labels',
    ):
        op.alter_column('demand_workload', column, nullable=False)
    op.alter_column('demand_deployment', 'admission_phase', nullable=False)

    op.drop_constraint('demand_deployment_workload_id_key', 'demand_deployment', type_='unique')
    op.drop_index(op.f('ix_demand_workload_cluster_id'), table_name='demand_workload')
    op.drop_index(op.f('ix_demand_workload_admission_phase'), table_name='demand_workload')
    op.drop_index(op.f('ix_demand_deployment_recipe_id'), table_name='demand_deployment')

    op.drop_column('demand_workload', 'cluster_id')
    op.drop_column('demand_workload', 'admission_phase')
    op.drop_column('demand_workload', 'admission_reason')
    op.drop_column('demand_workload', 'queued_at')
    op.drop_column('demand_workload', 'admitted_at')
    op.drop_column('demand_workload', 'placed_at')

    op.drop_column('demand_deployment', 'recipe_id')
    op.drop_column('demand_deployment', 'adapter_artifact_ids')
    op.drop_column('demand_deployment', 'overrides')
    op.drop_column('demand_deployment', 'labels')
    op.drop_column('demand_deployment', 'requires_single_topology_domain')
    op.drop_column('demand_deployment', 'topology_domain_kind')

    op.create_foreign_key(
        'demand_workload_recipe_id_fkey', 'demand_workload', 'demand_recipe', ['recipe_id'], ['id'],
        ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'demand_workload_pinned_cluster_id_fkey', 'demand_workload', 'supply_cluster',
        ['pinned_cluster_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        'bind_allocation_deployment_id_fkey', 'bind_allocation', 'demand_deployment',
        ['deployment_id'], ['id'], ondelete='CASCADE',
    )
    op.create_index(op.f('ix_demand_workload_recipe_id'), 'demand_workload', ['recipe_id'], unique=False)
    op.create_index(
        op.f('ix_demand_workload_pinned_cluster_id'), 'demand_workload', ['pinned_cluster_id'], unique=False
    )
    op.create_unique_constraint('uq_demand_workload_project_name', 'demand_workload', ['project_id', 'name'])
    op.create_index(op.f('ix_demand_deployment_workload_id'), 'demand_deployment', ['workload_id'], unique=False)
    op.create_index(
        op.f('ix_demand_deployment_admission_phase'), 'demand_deployment', ['admission_phase'], unique=False
    )
    op.create_unique_constraint(
        'uq_demand_deployment_workload_cluster', 'demand_deployment', ['workload_id', 'cluster_id']
    )
    op.create_index(op.f('ix_bind_allocation_deployment_id'), 'bind_allocation', ['deployment_id'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    fanned_out = bind.execute(
        sa.text(
            "SELECT workload_id FROM demand_deployment GROUP BY workload_id HAVING count(*) > 1"
        )
    ).scalars().all()
    if fanned_out:
        raise RuntimeError(
            "cannot downgrade: a multi-cluster workload cannot be represented in the one-to-one schema"
        )

    op.drop_index(op.f('ix_bind_allocation_deployment_id'), table_name='bind_allocation')
    op.drop_constraint('bind_allocation_deployment_id_fkey', 'bind_allocation', type_='foreignkey')
    op.drop_column('bind_allocation', 'deployment_id')

    op.drop_constraint('uq_demand_deployment_workload_cluster', 'demand_deployment', type_='unique')
    op.drop_index(op.f('ix_demand_deployment_admission_phase'), table_name='demand_deployment')
    op.drop_index(op.f('ix_demand_deployment_workload_id'), table_name='demand_deployment')
    op.drop_constraint('uq_demand_workload_project_name', 'demand_workload', type_='unique')
    op.drop_index(op.f('ix_demand_workload_pinned_cluster_id'), table_name='demand_workload')
    op.drop_index(op.f('ix_demand_workload_recipe_id'), table_name='demand_workload')

    op.add_column('demand_deployment', sa.Column('recipe_id', sa.UUID(), nullable=True))
    op.add_column(
        'demand_deployment',
        sa.Column('adapter_artifact_ids', postgresql.ARRAY(sa.UUID()), server_default='{}', nullable=True),
    )
    op.add_column(
        'demand_deployment',
        sa.Column('overrides', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=True),
    )
    op.add_column(
        'demand_deployment',
        sa.Column('labels', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=True),
    )
    op.add_column('demand_deployment', sa.Column('requires_single_topology_domain', sa.Boolean(), nullable=True))
    op.add_column('demand_deployment', sa.Column('topology_domain_kind', sa.String(length=32), nullable=True))

    op.add_column('demand_workload', sa.Column('cluster_id', sa.UUID(), nullable=True))
    op.add_column(
        'demand_workload', sa.Column('admission_phase', sa.String(length=16), server_default='pending', nullable=True)
    )
    op.add_column('demand_workload', sa.Column('admission_reason', sa.Text(), nullable=True))
    op.add_column('demand_workload', sa.Column('queued_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('demand_workload', sa.Column('admitted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('demand_workload', sa.Column('placed_at', sa.DateTime(timezone=True), nullable=True))

    op.execute(
        """
        UPDATE demand_deployment d SET
            recipe_id = w.recipe_id,
            adapter_artifact_ids = w.adapter_artifact_ids,
            overrides = w.overrides,
            labels = w.labels,
            requires_single_topology_domain = w.requires_single_topology_domain,
            topology_domain_kind = w.topology_domain_kind
        FROM demand_workload w WHERE d.workload_id = w.id
        """
    )
    op.execute(
        """
        UPDATE demand_workload w SET
            cluster_id = d.cluster_id,
            admission_phase = d.admission_phase,
            admission_reason = d.admission_reason,
            queued_at = d.queued_at,
            admitted_at = d.admitted_at,
            placed_at = d.placed_at
        FROM demand_deployment d WHERE d.workload_id = w.id
        """
    )

    op.alter_column('demand_deployment', 'recipe_id', nullable=False)
    op.alter_column('demand_deployment', 'adapter_artifact_ids', nullable=False)
    op.alter_column('demand_deployment', 'overrides', nullable=False)
    op.alter_column('demand_deployment', 'labels', nullable=False)
    op.alter_column('demand_workload', 'admission_phase', nullable=False)

    op.drop_column('demand_workload', 'topology_domain_kind')
    op.drop_column('demand_workload', 'requires_single_topology_domain')
    op.drop_column('demand_workload', 'labels')
    op.drop_column('demand_workload', 'overrides')
    op.drop_column('demand_workload', 'adapter_artifact_ids')
    op.drop_column('demand_workload', 'paused')
    op.drop_constraint('demand_workload_pinned_cluster_id_fkey', 'demand_workload', type_='foreignkey')
    op.drop_column('demand_workload', 'pinned_cluster_id')
    op.drop_column('demand_workload', 'max_clusters')
    op.drop_column('demand_workload', 'cluster_floors')
    op.drop_column('demand_workload', 'strategy')
    op.drop_column('demand_workload', 'desired_scale')
    op.drop_column('demand_workload', 'max_scale')
    op.drop_column('demand_workload', 'min_scale')
    op.drop_constraint('demand_workload_recipe_id_fkey', 'demand_workload', type_='foreignkey')
    op.drop_column('demand_workload', 'recipe_id')
    op.drop_column('demand_workload', 'name')

    op.drop_column('demand_deployment', 'placed_at')
    op.drop_column('demand_deployment', 'admitted_at')
    op.drop_column('demand_deployment', 'queued_at')
    op.drop_column('demand_deployment', 'admission_reason')
    op.drop_column('demand_deployment', 'admission_phase')

    op.create_foreign_key(
        'demand_deployment_recipe_id_fkey', 'demand_deployment', 'demand_recipe', ['recipe_id'], ['id'],
        ondelete='RESTRICT',
    )
    op.create_index(op.f('ix_demand_deployment_recipe_id'), 'demand_deployment', ['recipe_id'], unique=False)
    op.create_index(op.f('ix_demand_workload_admission_phase'), 'demand_workload', ['admission_phase'], unique=False)
    op.create_index(op.f('ix_demand_workload_cluster_id'), 'demand_workload', ['cluster_id'], unique=False)
    op.create_foreign_key(
        'demand_workload_cluster_id_fkey', 'demand_workload', 'supply_cluster', ['cluster_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_unique_constraint('demand_deployment_workload_id_key', 'demand_deployment', ['workload_id'])
