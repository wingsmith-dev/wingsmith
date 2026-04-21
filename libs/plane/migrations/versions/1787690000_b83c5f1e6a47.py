"""declared serving behaviour, committed traffic, benchmark asks and alert thresholds

Revision ID: b83c5f1e6a47
Revises: a71e4b09c3d2
Create Date: 2026-08-25 16:40:00.000000

Four tables and eight columns, all of them declared state.

`ops_benchmark_run` is the only table here that changes shape rather than appearing. It gains a
parent, and `deployment_id` and `cluster_id` become nullable so that a run against an endpoint
can exist at all — an endpoint-level measurement has no single deployment and no single cluster
by construction. Existing rows keep both, so the widening rewrites nothing and the new check
constraint holds for every row already present.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "b83c5f1e6a47"
down_revision: Union[str, None] = "a71e4b09c3d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "demand_artifact_baseline",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("artifact_id", sa.UUID(), nullable=False),
        sa.Column("dataset", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("tolerance_pct", sa.Numeric(precision=6, scale=3), server_default="3.0", nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.String(length=255), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["artifact_id"], ["demand_artifact.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
        sa.UniqueConstraint(
            "artifact_id", "dataset", "metric", name="uq_demand_artifact_baseline_dataset_metric"
        ),
    )
    op.create_index(
        "ix_demand_artifact_baseline_artifact_id", "demand_artifact_baseline", ["artifact_id"]
    )

    op.create_table(
        "bind_serving_quota",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("endpoint_id", sa.UUID(), nullable=False),
        sa.Column("committed_rpm", sa.Integer(), nullable=True),
        sa.Column("committed_tpm", sa.BigInteger(), nullable=True),
        sa.Column("max_concurrent_requests", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("burst_rpm", sa.Integer(), nullable=True),
        sa.Column("ramp_up_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("on_exceed", sa.String(length=16), server_default="reject", nullable=False),
        sa.Column("queue_timeout_ms", sa.Integer(), nullable=True),
        sa.Column("limit_scope", sa.String(length=16), server_default="per_replica", nullable=False),
        sa.Column("success_rate_objective", sa.Numeric(precision=6, scale=5), nullable=True),
        sa.Column(
            "availability_objective",
            sa.String(length=40),
            server_default="availability-2026.1",
            nullable=False,
        ),
        sa.Column("objective_window_seconds", sa.Integer(), server_default="600", nullable=False),
        sa.Column("enforced", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(["endpoint_id"], ["demand_endpoint.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
        sa.UniqueConstraint("endpoint_id"),
    )
    op.create_index("ix_bind_serving_quota_endpoint_id", "bind_serving_quota", ["endpoint_id"])

    op.create_table(
        "ops_benchmark_request",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("deployment_id", sa.UUID(), nullable=True),
        sa.Column("workload_id", sa.UUID(), nullable=True),
        sa.Column("endpoint_id", sa.UUID(), nullable=True),
        sa.Column("harness", sa.String(length=32), nullable=False),
        sa.Column("image", sa.String(length=512), server_default="", nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("mode", sa.String(length=16), server_default="single", nullable=False),
        sa.Column("criteria_version", sa.String(length=32), nullable=False),
        sa.Column("tier", sa.String(length=8), nullable=True),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("homogeneous", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(deployment_id IS NOT NULL)::int + (workload_id IS NOT NULL)::int "
            "+ (endpoint_id IS NOT NULL)::int = 1",
            name="ck_ops_benchmark_request_one_target",
        ),
        sa.ForeignKeyConstraint(["deployment_id"], ["demand_deployment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workload_id"], ["demand_workload.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["endpoint_id"], ["demand_endpoint.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
    )
    op.create_index("ix_ops_benchmark_request_level", "ops_benchmark_request", ["level"])
    op.create_index("ix_ops_benchmark_request_deployment_id", "ops_benchmark_request", ["deployment_id"])
    op.create_index("ix_ops_benchmark_request_workload_id", "ops_benchmark_request", ["workload_id"])
    op.create_index("ix_ops_benchmark_request_endpoint_id", "ops_benchmark_request", ["endpoint_id"])

    op.create_table(
        "ops_alert_threshold",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("rule", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_uid", sa.String(length=64), nullable=True),
        sa.Column("dimensions", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("objective", sa.String(length=32), nullable=True),
        sa.Column("lookback", sa.String(length=16), nullable=True),
        sa.Column("for_seconds", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
    )
    op.create_index("ix_ops_alert_threshold_rule", "ops_alert_threshold", ["rule"])
    op.create_index(
        "uq_ops_alert_threshold_scope",
        "ops_alert_threshold",
        ["rule", "subject_type", "subject_uid", "dimensions"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )

    op.add_column(
        "demand_artifact",
        sa.Column("upstream", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.add_column(
        "demand_recipe",
        sa.Column("serving", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.add_column(
        "demand_recipe",
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.add_column(
        "demand_endpoint",
        sa.Column("surface", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )

    op.add_column("ops_benchmark_run", sa.Column("request_id", sa.UUID(), nullable=True))
    op.add_column("ops_benchmark_run", sa.Column("endpoint_id", sa.UUID(), nullable=True))
    op.add_column(
        "ops_benchmark_run",
        sa.Column("serving", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.add_column("ops_benchmark_run", sa.Column("step_index", sa.Integer(), server_default="0", nullable=False))
    op.add_column("ops_benchmark_run", sa.Column("vantage", sa.String(length=64), nullable=True))
    op.add_column("ops_benchmark_run", sa.Column("discard", sa.Boolean(), server_default="false", nullable=False))
    op.create_foreign_key(
        "fk_ops_benchmark_run_request_id",
        "ops_benchmark_run",
        "ops_benchmark_request",
        ["request_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_ops_benchmark_run_endpoint_id",
        "ops_benchmark_run",
        "demand_endpoint",
        ["endpoint_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_ops_benchmark_run_request_id", "ops_benchmark_run", ["request_id"])
    op.create_index("ix_ops_benchmark_run_endpoint_id", "ops_benchmark_run", ["endpoint_id"])
    op.alter_column("ops_benchmark_run", "deployment_id", existing_type=sa.UUID(), nullable=True)
    op.alter_column("ops_benchmark_run", "cluster_id", existing_type=sa.UUID(), nullable=True)
    op.create_check_constraint(
        "ck_ops_benchmark_run_owner",
        "ops_benchmark_run",
        "deployment_id IS NOT NULL OR endpoint_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ops_benchmark_run_owner", "ops_benchmark_run", type_="check")
    op.execute("DELETE FROM ops_benchmark_run WHERE deployment_id IS NULL OR cluster_id IS NULL")
    op.alter_column("ops_benchmark_run", "cluster_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("ops_benchmark_run", "deployment_id", existing_type=sa.UUID(), nullable=False)
    op.drop_index("ix_ops_benchmark_run_endpoint_id", table_name="ops_benchmark_run")
    op.drop_index("ix_ops_benchmark_run_request_id", table_name="ops_benchmark_run")
    op.drop_constraint("fk_ops_benchmark_run_endpoint_id", "ops_benchmark_run", type_="foreignkey")
    op.drop_constraint("fk_ops_benchmark_run_request_id", "ops_benchmark_run", type_="foreignkey")
    op.drop_column("ops_benchmark_run", "discard")
    op.drop_column("ops_benchmark_run", "vantage")
    op.drop_column("ops_benchmark_run", "step_index")
    op.drop_column("ops_benchmark_run", "serving")
    op.drop_column("ops_benchmark_run", "endpoint_id")
    op.drop_column("ops_benchmark_run", "request_id")

    op.drop_column("demand_endpoint", "surface")
    op.drop_column("demand_recipe", "capabilities")
    op.drop_column("demand_recipe", "serving")
    op.drop_column("demand_artifact", "upstream")

    op.drop_index("uq_ops_alert_threshold_scope", table_name="ops_alert_threshold")
    op.drop_index("ix_ops_alert_threshold_rule", table_name="ops_alert_threshold")
    op.drop_table("ops_alert_threshold")

    op.drop_index("ix_ops_benchmark_request_endpoint_id", table_name="ops_benchmark_request")
    op.drop_index("ix_ops_benchmark_request_workload_id", table_name="ops_benchmark_request")
    op.drop_index("ix_ops_benchmark_request_deployment_id", table_name="ops_benchmark_request")
    op.drop_index("ix_ops_benchmark_request_level", table_name="ops_benchmark_request")
    op.drop_table("ops_benchmark_request")

    op.drop_index("ix_bind_serving_quota_endpoint_id", table_name="bind_serving_quota")
    op.drop_table("bind_serving_quota")

    op.drop_index("ix_demand_artifact_baseline_artifact_id", table_name="demand_artifact_baseline")
    op.drop_table("demand_artifact_baseline")
