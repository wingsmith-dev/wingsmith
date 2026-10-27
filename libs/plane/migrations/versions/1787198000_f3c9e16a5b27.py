"""let a deployment declare a topology requirement of its own

Revision ID: f3c9e16a5b27
Revises: e2b8d05c4f16
Create Date: 2026-08-20 00:30:00.000000

A recipe describes one replica of one engine, and its `requires_single_topology_domain`
is right for serving. A gang is a different shape of the same recipe (eight ranks that
have to share one NVLink fabric), and until now it had no way to say so, because the only
topology requirement in the system belonged to the recipe.

Both columns are nullable, and null is not false: it means "whatever the recipe says".
A non-null value overrides, so an existing deployment keeps placing exactly as it did.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = 'f3c9e16a5b27'
down_revision: Union[str, None] = 'e2b8d05c4f16'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('demand_deployment', sa.Column('requires_single_topology_domain', sa.Boolean(), nullable=True))
    op.add_column('demand_deployment', sa.Column('topology_domain_kind', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('demand_deployment', 'topology_domain_kind')
    op.drop_column('demand_deployment', 'requires_single_topology_domain')
