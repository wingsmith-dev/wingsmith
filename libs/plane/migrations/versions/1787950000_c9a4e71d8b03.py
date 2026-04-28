"""rename agent_version to operator_version

Revision ID: c9a4e71d8b03
Revises: 182ac5601e15
Create Date: 2026-08-28 09:00:00.000000

A pure rename, no shape change. The column always held the version of the
cluster operator, but "agent" is now the name of the cluster-side process that
carries inference requests over the tunnel, so the old name reads as a claim
about the wrong binary. Both the sync-state row and the per-node row carry it.

The wire field is renamed in the same change, so an operator built before this
sends `agent_version` and the hub will read it as absent. That is a version skew
of one deploy and it degrades to a NULL version, not an error.

"""

from typing import Union
from typing import Sequence

from alembic import op


revision: str = "c9a4e71d8b03"
down_revision: Union[str, None] = "182ac5601e15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("supply_cluster_sync_state", "agent_version", new_column_name="operator_version")
    op.alter_column("supply_node", "agent_version", new_column_name="operator_version")


def downgrade() -> None:
    op.alter_column("supply_node", "operator_version", new_column_name="agent_version")
    op.alter_column("supply_cluster_sync_state", "operator_version", new_column_name="agent_version")
