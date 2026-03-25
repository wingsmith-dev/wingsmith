"""let a recipe carry configuration the plane does not model

Revision ID: c5e8a3f1b742
Revises: a1c4f2b90e13
Create Date: 2026-08-19 23:30:00.000000

Engines and the Kubernetes operators that run them carry far more configuration than a
control plane wants to mirror field by field, and mirroring it is a losing race against
every engine release. `overlay` holds what the recipe deliberately does not model —
`pod_spec` merged over the rendered pod, `dynamo` over the rendered graph deployment —
and the plane never reads inside it.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'c5e8a3f1b742'
down_revision: Union[str, None] = 'a1c4f2b90e13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'demand_recipe',
        sa.Column('overlay', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('demand_recipe', 'overlay')
