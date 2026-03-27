"""make topology domain membership a table instead of one nullable column

Revision ID: a4d0f27b6c38
Revises: f3c9e16a5b27
Create Date: 2026-08-20 00:40:00.000000

`topology.observed` has always carried the full `node_ids`, but `supply_topology_domain`
held a single nullable `node_id`: one named node was stored, and a fabric spanning four
machines stored a null with `spans_nodes` true. "Which domains does this node belong to"
was therefore answerable only through the domain's devices, and "which nodes does this
domain span" not at all.

The scalar is dropped rather than kept alongside the join table. It would have to be
maintained in step with the rows, it would still be null for exactly the domains anyone
wants it for, and two spellings of one fact is how they drift apart. Existing single-node
domains are carried into the new table first, so nothing already observed is lost.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'a4d0f27b6c38'
down_revision: Union[str, None] = 'f3c9e16a5b27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'supply_topology_domain_node',
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('seq', sa.Integer(), sa.Identity(always=False, start=1000, cycle=True), nullable=False),
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('domain_id', sa.Uuid(), nullable=False),
        sa.Column('node_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['domain_id'], ['supply_topology_domain.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['node_id'], ['supply_node.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('domain_id', 'node_id', name='uq_supply_topology_domain_node'),
    )
    op.create_index(
        'ix_supply_topology_domain_node_domain_id', 'supply_topology_domain_node', ['domain_id'], unique=False
    )
    op.create_index(
        'ix_supply_topology_domain_node_node_id', 'supply_topology_domain_node', ['node_id'], unique=False
    )
    op.execute(
        "INSERT INTO supply_topology_domain_node (domain_id, node_id) "
        "SELECT id, node_id FROM supply_topology_domain WHERE node_id IS NOT NULL"
    )
    op.drop_index('ix_supply_topology_domain_node_id', table_name='supply_topology_domain')
    op.drop_column('supply_topology_domain', 'node_id')


def downgrade() -> None:
    op.add_column('supply_topology_domain', sa.Column('node_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'supply_topology_domain_node_id_fkey',
        'supply_topology_domain',
        'supply_node',
        ['node_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_index('ix_supply_topology_domain_node_id', 'supply_topology_domain', ['node_id'], unique=False)
    op.execute(
        "UPDATE supply_topology_domain d SET node_id = m.node_id "
        "FROM (SELECT domain_id, min(node_id::text)::uuid AS node_id FROM supply_topology_domain_node "
        "GROUP BY domain_id HAVING count(*) = 1) m WHERE m.domain_id = d.id"
    )
    op.drop_index('ix_supply_topology_domain_node_node_id', table_name='supply_topology_domain_node')
    op.drop_index('ix_supply_topology_domain_node_domain_id', table_name='supply_topology_domain_node')
    op.drop_table('supply_topology_domain_node')
