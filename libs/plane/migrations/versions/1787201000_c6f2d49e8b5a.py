"""carry the serving spec on a sighting, and mark artifacts adopted by reference

Revision ID: c6f2d49e8b5a
Revises: b5e1c38d7a49
Create Date: 2026-08-20 15:10:00.000000

A sighting recorded that a workload held eight devices and hashed to a fingerprint, and
nothing about how it had been told to serve. Everything a recipe needs was on the pod
template the whole time — engine and version in the image tag, context length and
parallelism in the container's args, cpu and memory in its requests — and none of it
crossed the wire. `spec` is that, stored as the cluster reported it and interpreted
nowhere near here.

It is not null with a `{}` default rather than nullable: a workload whose pod holds no
accelerator container has no serving spec to report, and an empty object says that
without asking every reader to handle a null first.

`artifact.verified` splits identity from provenance. An artifact's digest is its
identity, and adopting a model we found running gives us a reference — `hf:org/model`
from the container's `--model` — rather than the bytes. Those rows are real artifacts and
recipes may point at them; the flag is what stops them being mistaken for
content-addressed ones before anything has resolved the weights. Existing rows were
registered digest-first, so they default true.

"""
from typing import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = 'c6f2d49e8b5a'
down_revision: Union[str, None] = 'b5e1c38d7a49'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "demand_discovered_workload",
        sa.Column(
            "spec",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "demand_artifact",
        sa.Column("verified", sa.Boolean(), nullable=False, server_default="true"),
    )


def downgrade() -> None:
    op.drop_column("demand_artifact", "verified")
    op.drop_column("demand_discovered_workload", "spec")
