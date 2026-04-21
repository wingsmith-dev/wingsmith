"""a recipe carries a declared lifecycle: draft, published or deprecated

Revision ID: b2d3f4e5c6a7
Revises: a1c2e3d4b5f6
Create Date: 2026-08-26 12:10:00.000000

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'b2d3f4e5c6a7'
down_revision: Union[str, None] = 'a1c2e3d4b5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'demand_recipe',
        sa.Column(
            'lifecycle',
            sa.String(length=32),
            nullable=False,
            server_default='published',
        ),
    )
    op.create_index('ix_demand_recipe_lifecycle', 'demand_recipe', ['lifecycle'])


def downgrade() -> None:
    op.drop_index('ix_demand_recipe_lifecycle', table_name='demand_recipe')
    op.drop_column('demand_recipe', 'lifecycle')
