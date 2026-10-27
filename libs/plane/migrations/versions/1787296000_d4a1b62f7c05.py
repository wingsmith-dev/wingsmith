"""a deployment's observed sub-state is recorded beside its phase

Revision ID: d4a1b62f7c05
Revises: c3f8a05d4e69
Create Date: 2026-08-21 14:30:00.000000

`reason` and `message` on `demand_deployment_status` are what the cluster says it
is doing within a phase: the recipe has not arrived, the weights are still
downloading, no node advertises enough accelerators. The operator has always known
these and had nowhere to put them: they went to a log line in another cluster and
the plane showed `pending` with no explanation for as long as it lasted.

Intentionally not `last_error`. Waiting for a download is not an error, and a column
whose name says otherwise turns every ordinary wait into something a reader has to
decide is fine. `reason` is a closed set the operator owns (MESSAGES.md); this
column does not constrain it, so a value the plane has never heard of is stored and
displayed, not rejected.

Both are null on existing rows and stay null until the cluster reports again, which
is correct: nothing here knows what those deployments were waiting for.
"""

import sqlalchemy as sa
from alembic import op


revision = "d4a1b62f7c05"
down_revision = "c3f8a05d4e69"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("demand_deployment_status", sa.Column("reason", sa.String(length=64), nullable=True))
    op.add_column("demand_deployment_status", sa.Column("message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("demand_deployment_status", "message")
    op.drop_column("demand_deployment_status", "reason")
