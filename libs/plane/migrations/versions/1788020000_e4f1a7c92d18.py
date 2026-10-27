"""add multinode to cluster sync state

Revision ID: e4f1a7c92d18
Revises: d1c8b3a95e42
Create Date: 2026-08-31 23:55:00.000000

Whether a cluster can run a replica whose devices do not fit on one machine. It
needs a leader-worker backend, Grove or LeaderWorkerSet, and without one Dynamo
renders a multi-node replica as a single pod holding a fraction of the devices the
engine was told to expect: it starts, then hangs in collective initialisation with
nothing saying why.

This was a per-cluster chart value the operator was told, which made it a second
copy of a truth the cluster already holds, and the copy could be wrong in the
direction that hangs. The operator now reads the API groups it is served and
reports what it finds, so this lands on the sync-state row with the rest of what
is observed.

Unlike `read_only` this one is not inert: placement must not send a spanning
replica to a cluster that cannot run one. Defaults false, which is the right
answer for every existing row and the right reading of an operator too old to
send it: such an operator could not have detected a backend either.

"""

from typing import Union
from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "e4f1a7c92d18"
down_revision: Union[str, None] = "d1c8b3a95e42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "supply_cluster_sync_state",
        sa.Column("multinode", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("supply_cluster_sync_state", "multinode")
