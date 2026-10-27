"""authority is an explicit mark, and imported parameters are recorded apart from declared ones

Revision ID: c3f8a05d4e69
Revises: b2e7f94c3d58
Create Date: 2026-08-21 06:00:00.000000

Two columns, one idea: what a workload was observed to be is a different kind of
fact from what the plane asserts about it, and neither should be inferred from how
the object arrived (ADR 0009).

`demand_deployment.authority` is `observed` or `managed`. Until now the operator
decided by asking whether `adopted_workload_uid` was set, which made provenance a
standing prohibition: an adopted workload could never be managed, however clearly
somebody decided it should be.

The server default is `observed`, so a row written by any path that has not thought
about the question fails closed. Existing rows are backfilled from the rule the
operator was already applying: `managed` where `adopted_workload_uid` is null,
`observed` where it is set. That is behaviour-preserving, not
tidy. A blanket `observed` would stop the plane converging deployments it created
and is converging today; a blanket `managed` would start it converging four adopted
production workloads because a migration ran. Both are changes nobody asked this
migration to make.

`demand_recipe.imported_args` is what a running process was read to be using, kept
beside `engine_args`, which is what the plane declares. Same shape, different kind.
Only `engine_args` renders onto a command line; `imported_args` is a record and is
never enforced. Empty for every existing row, not backfilled out of
`engine_args`: those maps hold a mix of both, and guessing which half was observed
would manufacture provenance nobody recorded.

No backfill for the `--max-num-seqs` mis-mapping this ADR also fixes. Recipes
drafted from a running vLLM workload carry that value in `max_batch_tokens`, where
it means something else entirely, and correcting it changes a serving parameter:
that is a decision to make per recipe with a person looking, not a side effect of
upgrading a schema.
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "c3f8a05d4e69"
down_revision: str | None = "b2e7f94c3d58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "demand_deployment",
        sa.Column("authority", sa.String(length=16), server_default="observed", nullable=False),
    )
    op.execute(
        "UPDATE demand_deployment SET authority = 'managed' WHERE adopted_workload_uid IS NULL"
    )
    op.create_index("ix_demand_deployment_authority", "demand_deployment", ["authority"])
    op.add_column(
        "demand_recipe",
        sa.Column("imported_args", sa.dialects.postgresql.JSONB(), server_default="{}", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("demand_recipe", "imported_args")
    op.drop_index("ix_demand_deployment_authority", table_name="demand_deployment")
    op.drop_column("demand_deployment", "authority")
