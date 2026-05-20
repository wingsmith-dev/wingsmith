"""served model name override on endpoint targets

Revision ID: a3c7e1f45b92
Revises: b6d2f8a41c07
Create Date: 2026-09-01 12:00:00.000000

The routing table is the plane's, and until now it recomputed what the engine
answers to on every read — walking `engine_args`, the workload, the recipe and
the artifact — while the operator derived the same string a second time from its
own copy of the rule. Two derivations of one value drift, and did: a recipe
carrying an `engine_args` override served `moonshotai/kimi-k3` while the table
published the fit name, so the gateway rewrote the inbound `model` to a string
the frontend had never heard of and every request 404'd from inside the cluster.

This column is the correction, and it is deliberately at the routing table rather
than anywhere that renders a command line. The engine's name is invisible to
clients: nothing outside the cluster types it and the gateway rewrites into it,
so the only property that matters is that this table and that process agree.
Making the agreement writable here means fixing a disagreement costs a poll,
where fixing it on the recipe costs a pod respec and a full reload of the weights
— twenty-two minutes for a checkpoint the size of Kimi K3.

No backfill. Null means "ask the deployment", which is exactly what every row
does today, so this migration changes no behaviour on its own.

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
