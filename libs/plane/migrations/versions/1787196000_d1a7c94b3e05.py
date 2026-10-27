"""drop the cost ceiling nothing can enforce

Revision ID: d1a7c94b3e05
Revises: c5e8a3f1b742
Create Date: 2026-08-20 00:10:00.000000

`bind_policy.max_hourly_cost` was a ceiling with nothing underneath it: no rate is
recorded on a device, a pool or a cluster, so placement has no number to compare a
candidate against and never read the column. A constraint that cannot be evaluated is
worse than an absent one: it reads as enforced. Pricing, when supply learns about it,
will want a rate per device class on the supply side first, and the ceiling can come
back then against something real.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'd1a7c94b3e05'
down_revision: Union[str, None] = 'c5e8a3f1b742'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('bind_policy', 'max_hourly_cost')


def downgrade() -> None:
    op.add_column('bind_policy', sa.Column('max_hourly_cost', sa.Numeric(precision=12, scale=4), nullable=True))
