"""benchmark history outlives the deployment, workload or endpoint it measured

Revision ID: ee98c5cc5d50
Revises: 730d51320227
Create Date: 2026-10-15 10:30:00.000000

A drain, or a re-expansion that leaves a cluster with no share, deletes that cluster's
deployment. Every benchmark run and request pointing at it was deleted with it, because
the foreign keys cascaded, so the parameter-tuning record lost exactly the runs that
explained why a workload moved. Deleting a workload or an endpoint did the same.

The references become `ON DELETE SET NULL`. A run keeps `serving_digest`, `engine_args`,
`cluster_id` and `model`, which is what it needs to say what it measured after the
deployment is gone.

The check constraints that required a target are relaxed to match: a run no longer has
to name a deployment or an endpoint, and a request names at most one target. That a new
request or run names exactly the right one is checked by the service when it is created.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "ee98c5cc5d50"
down_revision: str | None = "730d51320227"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


REFERENCES = (
    ("ops_benchmark_run", "ops_benchmark_run_deployment_id_fkey", "deployment_id", "demand_deployment"),
    ("ops_benchmark_run", "fk_ops_benchmark_run_endpoint_id", "endpoint_id", "demand_endpoint"),
    ("ops_benchmark_request", "ops_benchmark_request_deployment_id_fkey", "deployment_id", "demand_deployment"),
    ("ops_benchmark_request", "ops_benchmark_request_workload_id_fkey", "workload_id", "demand_workload"),
    ("ops_benchmark_request", "ops_benchmark_request_endpoint_id_fkey", "endpoint_id", "demand_endpoint"),
)

ONE_TARGET = "(deployment_id IS NOT NULL)::int + (workload_id IS NOT NULL)::int + (endpoint_id IS NOT NULL)::int"


def _repoint(ondelete: str) -> None:
    for table, name, column, target in REFERENCES:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, target, [column], ["id"], ondelete=ondelete)


def upgrade() -> None:
    _repoint("SET NULL")
    op.drop_constraint("ck_ops_benchmark_run_owner", "ops_benchmark_run", type_="check")
    op.drop_constraint("ck_ops_benchmark_request_one_target", "ops_benchmark_request", type_="check")
    op.create_check_constraint("ck_ops_benchmark_request_one_target", "ops_benchmark_request", f"{ONE_TARGET} <= 1")


def downgrade() -> None:
    """Restores the cascades. Runs and requests whose target was deleted meanwhile are
    deleted here, because the restored check constraints cannot hold them."""
    op.execute("DELETE FROM ops_benchmark_run WHERE deployment_id IS NULL AND endpoint_id IS NULL")
    op.execute(f"DELETE FROM ops_benchmark_request WHERE {ONE_TARGET} = 0")
    op.drop_constraint("ck_ops_benchmark_request_one_target", "ops_benchmark_request", type_="check")
    op.create_check_constraint("ck_ops_benchmark_request_one_target", "ops_benchmark_request", f"{ONE_TARGET} = 1")
    op.create_check_constraint(
        "ck_ops_benchmark_run_owner", "ops_benchmark_run", "deployment_id IS NOT NULL OR endpoint_id IS NOT NULL"
    )
    _repoint("CASCADE")
