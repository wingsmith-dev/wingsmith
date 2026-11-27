"""no alias may equal another endpoint's slug

Revision ID: 8d3f6c0e2b57
Revises: 7c2e5b9d1a46
Create Date: 2026-11-27 16:00:00.000000

The gateway indexes slugs and aliases into one route table, so an alias that is another
endpoint's slug makes one of the two unreachable. 6b9d4f2a8c35 only checked slug against slug.
Existing clashes stop the upgrade with the list; which endpoint keeps the id is an operator
decision. Nothing in the schema changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "8d3f6c0e2b57"
down_revision: str | None = "7c2e5b9d1a46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    clashes = op.get_bind().execute(
        sa.text(
            "SELECT a.alias, owner.uid, e.uid FROM demand_endpoint_alias a "
            "JOIN demand_endpoint owner ON owner.id = a.endpoint_id "
            "JOIN demand_endpoint e ON e.slug = a.alias AND e.id <> a.endpoint_id "
            "ORDER BY a.alias"
        )
    ).all()
    if clashes:
        listing = "; ".join(f"{alias!r}: alias of {owner}, slug of {other}" for alias, owner, other in clashes)
        raise RuntimeError(
            f"endpoint aliases equal another endpoint's slug: {listing}. "
            "Remove the alias or change the slug (PATCH /v1/endpoints/{id}) and rerun the migration."
        )


def downgrade() -> None:
    pass
