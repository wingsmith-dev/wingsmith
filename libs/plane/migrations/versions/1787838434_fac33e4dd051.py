"""cluster enrolment token hash

Revision ID: fac33e4dd051
Revises: b2d3f4e5c6a7
Create Date: 2026-08-27 10:47:14.329199

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'fac33e4dd051'
down_revision: Union[str, None] = 'b2d3f4e5c6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('supply_cluster', sa.Column('token_hash', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_supply_cluster_token_hash'), 'supply_cluster', ['token_hash'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_supply_cluster_token_hash'), table_name='supply_cluster')
    op.drop_column('supply_cluster', 'token_hash')
