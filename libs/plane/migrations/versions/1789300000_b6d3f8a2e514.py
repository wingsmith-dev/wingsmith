"""a workload can declare that prefill and decode are separate processes

Revision ID: b6d3f8a2e514
Revises: c2e8a41f7b06
Create Date: 2026-09-07 18:00:00.000000

Additive throughout, and that is the requirement, not a convenience. Every workload in
the fleet is `aggregated` with no role rows, and the object rendered for it must be byte-identical
to what it produces today: the same bar `FrontendEnabled` sets for its own false case.

`demand_workload.serving_topology` is `NOT NULL` with a server default, not nullable. There
is no third state: a workload either is disaggregated or is not, and a null would invent a "not
yet decided" that nothing can render. The default is also what keeps the digest still: only a
`disaggregated` workload contributes `serving_topology` and its role configuration to
`serving_digest`, so no existing row's hash moves and no deployment's `ServingDigestAnnotation`
changes underneath a running cluster.

`demand_workload_role` is narrow because the delta between the roles is narrow. Across NVIDIA's
`disagg-gb300-agentic` recipe the two differ on six arguments and agree on the artifact, the
tensor degree, the device class, the context length, the quantization, the `kv-cache-dtype` and
the `kv-transfer-config`. That last one is the same string on both sides, because the transfer
mechanism is a property of the topology and not of the role. So a role is one weight, one optional
`max_batch_tokens` and an argument map.

`weight` is scale and not configuration, which is why it is here and not in the hash.
`ops_benchmark_run.serving_digest` is what links a measurement to the configuration it was taken
against; a weight in the digest would orphan every benchmark on a model the moment somebody added
a prefill worker, and those benchmarks are exactly what anyone choosing a ratio would consult.
"""

from typing import Union, Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b6d3f8a2e514"
down_revision: Union[str, None] = "c2e8a41f7b06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "demand_workload",
        sa.Column("serving_topology", sa.String(length=16), server_default="aggregated", nullable=False),
    )
    op.create_index("ix_demand_workload_serving_topology", "demand_workload", ["serving_topology"])

    op.create_table(
        "demand_workload_role",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workload_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("weight", sa.Integer(), server_default="1", nullable=False),
        sa.Column("max_batch_tokens", sa.Integer(), nullable=True),
        sa.Column("engine_args", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.ForeignKeyConstraint(["workload_id"], ["demand_workload.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workload_id", "role", name="uq_demand_workload_role_workload_role"),
    )
    op.create_index("ix_demand_workload_role_workload_id", "demand_workload_role", ["workload_id"])


def downgrade() -> None:
    op.drop_index("ix_demand_workload_role_workload_id", table_name="demand_workload_role")
    op.drop_table("demand_workload_role")
    op.drop_index("ix_demand_workload_serving_topology", table_name="demand_workload")
    op.drop_column("demand_workload", "serving_topology")
