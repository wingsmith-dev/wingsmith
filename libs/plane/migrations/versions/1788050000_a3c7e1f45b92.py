"""served model name override on endpoint targets

Revision ID: a3c7e1f45b92
Revises: b6d2f8a41c07
Create Date: 2026-09-01 12:00:00.000000

The routing table recomputed what the engine answers to on every read while the
operator derived the same string from its own copy of the rule, and the two could
disagree (for example when `engine_args` carries a `--served-model-name` override).
When they did, the gateway rewrote the inbound `model` to a name the frontend did not
serve.

This column is the correction, at the routing table, not anywhere that renders
a command line: fixing a disagreement costs a gateway poll, not a pod respec and a full
reload of the weights.

No backfill. Null means "ask the deployment", which is what every row does today.
"""

from typing import Union
from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "a3c7e1f45b92"
down_revision: Union[str, None] = "b6d2f8a41c07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("demand_endpoint_target", sa.Column("served_model_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("demand_endpoint_target", "served_model_name")
