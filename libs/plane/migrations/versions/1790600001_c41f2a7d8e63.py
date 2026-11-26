"""deployments render under a cluster-safe object name

Revision ID: c41f2a7d8e63
Revises: 3b7e5d1c9a42
Create Date: 2026-11-26 11:00:00.000000

A deployment was unique on `(cluster_id, name)` and `name` is the workload's, so two projects could
not both run a `qwen` on one cluster. `object_name` is what the deployment renders as in its
cluster, and the uniqueness moves to it; `name` stays the workload's, for people and references.

Existing rows keep their name as their object name: renaming would make the operator render a
second object beside the one already running. New deployments get the workload-uid-suffixed name
from `app.modules.deployments.naming`.

The downgrade refuses while two deployments in one cluster share a name, since the old constraint
cannot hold them.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "c41f2a7d8e63"
down_revision: str | None = "3b7e5d1c9a42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("demand_deployment", sa.Column("object_name", sa.String(length=63), nullable=True))
    op.execute("UPDATE demand_deployment SET object_name = name")
    op.alter_column("demand_deployment", "object_name", nullable=False)
    op.drop_constraint("uq_demand_deployment_cluster_name", "demand_deployment", type_="unique")
    op.create_unique_constraint(
        "uq_demand_deployment_cluster_object_name", "demand_deployment", ["cluster_id", "object_name"]
    )


def downgrade() -> None:
    clash = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT string_agg(uid, ', ') FROM demand_deployment d WHERE EXISTS ("
                " SELECT 1 FROM demand_deployment o WHERE o.cluster_id = d.cluster_id"
                " AND o.name = d.name AND o.id <> d.id)"
            )
        )
        .scalar()
    )
    if clash:
        raise RuntimeError(f"deployments share a name within one cluster, delete or rename them first: {clash}")
    op.drop_constraint("uq_demand_deployment_cluster_object_name", "demand_deployment", type_="unique")
    op.create_unique_constraint("uq_demand_deployment_cluster_name", "demand_deployment", ["cluster_id", "name"])
    op.drop_column("demand_deployment", "object_name")
