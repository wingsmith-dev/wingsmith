"""a model has one name for machines and one for people

Revision ID: e4b9d2a15c73
Revises: a7c31f8e64b2
Create Date: 2026-09-02 15:40:00.000000

Five columns across four tables held a version of "what is this model called", and no two of them
had to agree.

`Artifact` held only `name`, and it was a bare `gpt-oss-20b` with no organisation, because the
organisation lives in the location URI and nothing read it. So `app.core.serving` reconstructed a
slug at read time from `name`, else a `hf:`-prefixed `digest`, else the workload's name — three
guesses, none of which could produce `openai/`. Meanwhile the operator started every engine on
`--model` rendered from that same location, which had the organisation all along. The gateway then
rewrote inbound requests to the plane's answer, and the frontend had registered the operator's.

The failure is not visible from either end. The deployment is healthy, the worker is ready, the
tunnel is open, and the request comes back `model_not_found` from inside the cluster. Exactly one
model in the fleet served, and only because a person had typed the correct answer by hand into
`Workload.served_model_name`.

So the slug is stored once, on the artifact, derived on the way in from the location that names it:
`hf://moonshotai/Kimi-K3` becomes `moonshotai/kimi-k3`. Lowercase because that is what a caller
types; the organisation kept because a bare `kimi-k3` is not a model id anybody can look up. That
the backfill independently reproduces the one hand-typed value that worked is the evidence it is
the right rule.

`demand_workload.served_model_name` is dropped and not replaced. A second writeable spelling of a
public identifier is how the id a client sends and the id a frontend registered come apart, and the
workload is the wrong object to hold it anyway — it is a fact about the weights, so it survives
re-fitting the same model at a different tensor degree, which naming it after the configuration did
not. What a *client* sends is still changeable: that is `demand_endpoint.slug`, patchable, and the
gateway rewrites between the two on its next poll — no engine flag, no pod respec, nothing on any
cluster.

`demand_endpoint.model_slug` becomes `slug` and stops being nullable. It was null-means-`name`,
which forced a second field, `resolved_model_slug`, computed on the way out purely so an operator
could see which of the two had won. `display_name` is dropped: it was a third name for the same
object, written by nothing and read by one console component.

Backfills are `COALESCE` chains rather than a data migration script because every source is already
in the database and none of them needs a network call. An artifact whose locations say nothing
resolvable and whose digest is a content hash keeps a lowercased `name` — wrong in the same way it
was wrong before, and now visible in a column instead of hidden in a function.
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
