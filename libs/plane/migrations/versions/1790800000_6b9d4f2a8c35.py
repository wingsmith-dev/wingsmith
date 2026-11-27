"""endpoint names are unique per project, slugs globally

Revision ID: 6b9d4f2a8c35
Revises: 5a8c3e1f7b24
Create Date: 2026-11-27 12:00:00.000000

`name` moves from globally unique to `(project_id, name)`, like a workload's. `slug` is the routing
key the gateway reads, and two endpoints sharing one used to be accepted and then routed to whichever
the gateway indexed first; its index becomes unique. Existing duplicate slugs stop the upgrade with
the list, instead of being renamed here: which endpoint keeps the model id is an operator decision.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "6b9d4f2a8c35"
down_revision: str | None = "5a8c3e1f7b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT slug, string_agg(uid, ', ' ORDER BY uid) FROM demand_endpoint "
            "GROUP BY slug HAVING count(*) > 1 ORDER BY slug"
        )
    ).all()
    if duplicates:
        listing = "; ".join(f"{slug!r}: {uids}" for slug, uids in duplicates)
        raise RuntimeError(
            f"endpoints share a slug, which must now be unique: {listing}. "
            "Give all but one of each a different slug (PATCH /v1/endpoints/{id}) and rerun the migration."
        )
    op.drop_constraint("demand_endpoint_name_key", "demand_endpoint", type_="unique")
    op.create_unique_constraint("uq_demand_endpoint_project_name", "demand_endpoint", ["project_id", "name"])
    op.drop_index("ix_demand_endpoint_slug", table_name="demand_endpoint")
    op.create_index("ix_demand_endpoint_slug", "demand_endpoint", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_demand_endpoint_slug", table_name="demand_endpoint")
    op.create_index("ix_demand_endpoint_slug", "demand_endpoint", ["slug"])
    op.drop_constraint("uq_demand_endpoint_project_name", "demand_endpoint", type_="unique")
    op.create_unique_constraint("demand_endpoint_name_key", "demand_endpoint", ["name"])
