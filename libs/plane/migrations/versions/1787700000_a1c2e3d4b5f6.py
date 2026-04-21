"""a node retains the full observed condition set, not only the reduced summary

Revision ID: a1c2e3d4b5f6
Revises: b83c5f1e6a47
Create Date: 2026-08-26 12:00:00.000000

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = 'a1c2e3d4b5f6'
down_revision: Union[str, None] = 'b83c5f1e6a47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'supply_node',
        sa.Column(
            'conditions',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='[]',
        ),
    )


def downgrade() -> None:
    op.drop_column('supply_node', 'conditions')
