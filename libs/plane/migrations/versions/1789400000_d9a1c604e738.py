"""per-role observed readiness reaches the plane

Revision ID: d9a1c604e738
Revises: b6d3f8a2e514
Create Date: 2026-09-08 10:00:00.000000

Additive, and both columns are written by ingestion and by nothing else. The previous migration
gave a workload its declared roles; this is the observed half coming back — which role a running
replica turned out to be, and how many of each role its cluster has ready against how many it
rendered.

`demand_deployment_status.roles` is a JSONB array rather than a table, because nothing joins it,
filters on it or aggregates it: it is read whole with its deployment and displayed. It is stored
rather than derived from `demand_replica.role` because the derivation is only half available — a
rollup over replica rows can say how many prefill workers exist and never how many were asked for,
and the second number is the one that makes a shortfall legible. Deriving one and storing the other
would also split one internally consistent frame across two sources read at different instants,
which is the reason the scalar counts beside it are columns rather than a rollup already.

`demand_replica.role` is nullable rather than defaulted to `decode`. "This deployment has one
worker" and "this worker is the decode half of a pair" are different facts, and a default would
erase the difference on every row that predates the column.
"""

from typing import Union, Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d9a1c604e738"
down_revision: Union[str, None] = "b6d3f8a2e514"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "demand_deployment_status",
        sa.Column("roles", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
    )
    op.add_column("demand_replica", sa.Column("role", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("demand_replica", "role")
    op.drop_column("demand_deployment_status", "roles")
