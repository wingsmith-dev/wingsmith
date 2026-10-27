"""add read_only to cluster sync state

Revision ID: d1c8b3a95e42
Revises: c9a4e71d8b03
Create Date: 2026-08-31 20:30:00.000000

A read-only operator runs without its reconcile loop: it reports everything the
cluster has and applies nothing the plane declares. That was invisible from the
plane, so placement would pick such a cluster, the hub would stream the desired
state, and the workload would sit at zero replicas with no error anywhere.

Observed, so it lands on the sync-state row instead of the registry row:
only the operator knows it, and `Cluster` has exactly one writer that is not
ingestion. Defaults false, which is both the right answer for every existing row
and the right reading of an operator too old to send the field.

"""

from typing import Union
from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "d1c8b3a95e42"
down_revision: Union[str, None] = "c9a4e71d8b03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "supply_cluster_sync_state",
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("supply_cluster_sync_state", "read_only")
