"""a model has one name for machines and one for people

Revision ID: e4b9d2a15c73
Revises: a7c31f8e64b2
Create Date: 2026-09-02 15:40:00.000000

Five columns across four tables held a version of "what is this model called", and no
two of them had to agree. `Artifact.name` was a bare `gpt-oss-20b` with no organisation,
because the organisation lives in the location URI and nothing read it, while the
operator started every engine on `--model` rendered from that location. The gateway
then rewrote inbound requests to a name the frontend had not registered.

So the slug is stored once, on the artifact, derived on the way in from the location
that names it: `hf://openai/gpt-oss-20b` becomes `openai/gpt-oss-20b`. Lowercase because
that is what a caller types; the organisation is kept because a bare model name is not
an id anybody can look up.

`demand_workload.served_model_name` is dropped and not replaced: the served name is a
fact about the weights, and what a client sends is still changeable on
`demand_endpoint.slug`, which the gateway rewrites between.

`demand_endpoint.model_slug` becomes `slug` and stops being nullable. `display_name` is
dropped: it was a third name for the same object.

Backfills are `COALESCE` chains because every source is already in the database. An
artifact whose locations say nothing resolvable and whose digest is a content hash
keeps a lowercased `name`.
"""

from typing import Union, Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "e4b9d2a15c73"
down_revision: Union[str, None] = "a7c31f8e64b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The preferred location, else the oldest, reduced to `org/model`.
#
# `split_part(uri, '://', 2)` takes what follows the scheme, and the `@revision` suffix is trimmed
# because a pinned commit is which bytes, not what the model is called — two revisions of one
# repository are the same model id and must resolve to one endpoint.
ARTIFACT_SLUG_FROM_LOCATION = """
    SELECT lower(regexp_replace(
               trim(both '/' from split_part(l.uri, '://', 2)), '@[^@/]*$', ''))
      FROM demand_artifact_location l
     WHERE l.artifact_id = a.id
       AND position('://' in l.uri) > 0
     ORDER BY l.preferred DESC, l.seq ASC
     LIMIT 1
"""

# An artifact adopted by watching a cluster serve it has no location: the reference is the digest,
# as `hf:org/model`, which is also why such a row is `verified = false`.
ARTIFACT_SLUG_FROM_DIGEST = """
    CASE WHEN lower(a.digest) LIKE 'hf:%%'
         THEN lower(regexp_replace(trim(both '/' from substring(a.digest from 4)), '@[^@/]*$', ''))
    END
"""


def upgrade() -> None:
    op.add_column(
        "demand_artifact",
        sa.Column("slug", sa.String(length=255), nullable=False, server_default=""),
    )
    op.create_index("ix_demand_artifact_slug", "demand_artifact", ["slug"])
    op.execute(
        f"""
        UPDATE demand_artifact a
           SET slug = COALESCE(
                   NULLIF(({ARTIFACT_SLUG_FROM_LOCATION}), ''),
                   NULLIF(({ARTIFACT_SLUG_FROM_DIGEST}), ''),
                   lower(a.name))
        """
    )

    op.add_column(
        "demand_endpoint",
        sa.Column("slug", sa.String(length=255), nullable=False, server_default=""),
    )
    op.create_index("ix_demand_endpoint_slug", "demand_endpoint", ["slug"])
    op.execute(
        """
        UPDATE demand_endpoint
           SET slug = lower(COALESCE(NULLIF(trim(model_slug), ''), name))
        """
    )
    op.drop_column("demand_endpoint", "model_slug")
    op.drop_column("demand_endpoint", "display_name")

    op.drop_column("demand_workload", "served_model_name")


def downgrade() -> None:
    # `served_model_name` comes back empty. It was an override of a value now stored on the
    # artifact, so every row that had one has had its answer folded into the slug the artifact
    # carries; restoring the column with the old strings would reinstate the disagreement this
    # revision exists to remove.
    op.add_column(
        "demand_workload",
        sa.Column("served_model_name", sa.String(length=255), nullable=True),
    )

    op.add_column("demand_endpoint", sa.Column("display_name", sa.String(length=255), nullable=True))
    op.add_column("demand_endpoint", sa.Column("model_slug", sa.String(length=255), nullable=True))
    op.execute("UPDATE demand_endpoint SET model_slug = NULLIF(slug, name)")
    op.create_index("ix_demand_endpoint_model_slug", "demand_endpoint", ["model_slug"])
    op.drop_index("ix_demand_endpoint_slug", table_name="demand_endpoint")
    op.drop_column("demand_endpoint", "slug")

    op.drop_index("ix_demand_artifact_slug", table_name="demand_artifact")
    op.drop_column("demand_artifact", "slug")
