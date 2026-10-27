"""a benchmark's stdout tail crosses the wire

Revision ID: a4f5b1c72d99
Revises: f1a7c94e2b68
Create Date: 2026-09-03 17:10:00.000000

`BenchmarkRun` carries its harness's summary and nothing else from the pod that produced it:
enough when the summary arrived whole, and opaque when it did not. The sentinel-framed JSON was
the only channel, so a run whose evalscope crashed before writing its report was a `succeeded`
phase with no number beside it, or a `failed` phase with only the operator's interpretation of
the error.

The operator now reads the pod's log the same way it already does to find the summary, and the
last ~64 KiB of it crosses the wire on `benchmark.observed` beside the summary. That is enough
to reconstruct a truncated summary, a judge refusal, or a tokenizer resolution miss without
shell access to a cluster somebody else owns. A run that wants more than the tail should publish
under `artifacts_uri`; the wire is not the persistent channel and was not made one here.
"""

from typing import Union, Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a4f5b1c72d99"
down_revision: Union[str, None] = "f1a7c94e2b68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ops_benchmark_run", sa.Column("log_tail", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ops_benchmark_run", "log_tail")
