"""cluster remote write hash

Revision ID: 182ac5601e15
Revises: b719e4e78b13
Create Date: 2026-08-27 19:39:38.674924

A second verifier over the enrolment token: bcrypt this time, because Traefik's
`basicAuth` middleware in front of the central metrics store's write ingress can
only check a crypt-style hash, while the hub needs the indexed sha256 it already
has. One credential, two hashes, both written at mint (ADR 0012).

Existing rows get NULL and **cannot be backfilled**. The plaintext token exists
only at the moment it is minted, so there is nothing to derive a bcrypt hash
from. The consequence is concrete: a cluster registered before this migration
does not appear in the htpasswd the reactor renders, and its metrics agent gets a
401 on every remote-write until someone re-issues its enrolment token
(`POST /v1/clusters/<ref>/enrolment-token`) and reinstalls the chart with the new
values. That is a deliberate cost: the alternative is a second, separate
credential for telemetry, which is the per-cluster manual step this whole change
exists to remove.
"""

from typing import Union
from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "182ac5601e15"
down_revision: Union[str, None] = "b719e4e78b13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("supply_cluster", sa.Column("remote_write_hash", sa.String(length=72), nullable=True))


def downgrade() -> None:
    op.drop_column("supply_cluster", "remote_write_hash")
