"""public model slug on endpoints

Revision ID: c5e9a2b73f41
Revises: a3c7e1f45b92
Create Date: 2026-09-01 06:00:00.000000

The model id a client sends was the endpoint's name, and the endpoint's name is not
patchable: it is the internal handle in URLs, metrics labels and console links. Those
are two jobs for one string.

`model_slug` splits them. Null means the name still stands, so this changes nothing on
its own. Where it is set it becomes what the resolved table reports as the endpoint's
name, which is the key the gateway builds its route map on.

Indexed and not unique: `Endpoint.name` is globally unique only while there is one
tenant, and this column has to follow whatever scoping that becomes.
"""

from typing import Union
from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "c5e9a2b73f41"
down_revision: Union[str, None] = "a3c7e1f45b92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("demand_endpoint", sa.Column("model_slug", sa.String(length=255), nullable=True))
    op.create_index("ix_demand_endpoint_model_slug", "demand_endpoint", ["model_slug"])


def downgrade() -> None:
    op.drop_index("ix_demand_endpoint_model_slug", table_name="demand_endpoint")
    op.drop_column("demand_endpoint", "model_slug")
