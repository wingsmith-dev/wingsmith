"""a fleet-wide quota or credential is unique in its scope

Revision ID: 730d51320227
Revises: b6f96e7c388b
Create Date: 2026-10-15 10:20:00.000000

`uq_bind_quota_scope` is `(project_id, cluster_id, device_class)` and `cluster_id` is null
for a quota that applies across the fleet. `uq_ops_credential_scope_name` is
`(scope_type, scope_id, name)` and `scope_id` is null for a fleet-scoped credential. Under
the default rule two nulls are distinct, so neither constraint stopped two fleet-wide
rows for the same scope, and which one placement or the gateway read was whichever the
planner returned. Both become `NULLS NOT DISTINCT` (Postgres 15 or later).

The upgrade refuses, and lists the rows, if duplicates already exist: picking which
quota or credential survives is the operator's decision, not the migration's.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "730d51320227"
down_revision: str | None = "b6f96e7c388b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


QUOTA_DUPLICATES = """
    SELECT string_agg(uid, ', ' ORDER BY uid)
      FROM bind_quota
     WHERE cluster_id IS NULL
     GROUP BY project_id, device_class
    HAVING count(*) > 1
"""

CREDENTIAL_DUPLICATES = """
    SELECT string_agg(id::text, ', ' ORDER BY id)
      FROM ops_credential
     WHERE scope_id IS NULL
     GROUP BY scope_type, name
    HAVING count(*) > 1
"""


def _refuse_duplicates(query: str, what: str) -> None:
    groups = list(op.get_bind().execute(sa.text(query)).scalars())
    if groups:
        raise RuntimeError(
            f"cannot migrate: {len(groups)} set(s) of duplicate fleet-wide {what} exist; "
            f"delete all but one in each before upgrading: {'; '.join(groups)}"
        )


def upgrade() -> None:
    _refuse_duplicates(QUOTA_DUPLICATES, "quotas (bind_quota)")
    _refuse_duplicates(CREDENTIAL_DUPLICATES, "credentials (ops_credential)")

    op.drop_constraint("uq_bind_quota_scope", "bind_quota", type_="unique")
    op.create_unique_constraint(
        "uq_bind_quota_scope",
        "bind_quota",
        ["project_id", "cluster_id", "device_class"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_constraint("uq_ops_credential_scope_name", "ops_credential", type_="unique")
    op.create_unique_constraint(
        "uq_ops_credential_scope_name",
        "ops_credential",
        ["scope_type", "scope_id", "name"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_constraint("uq_ops_credential_scope_name", "ops_credential", type_="unique")
    op.create_unique_constraint("uq_ops_credential_scope_name", "ops_credential", ["scope_type", "scope_id", "name"])
    op.drop_constraint("uq_bind_quota_scope", "bind_quota", type_="unique")
    op.create_unique_constraint("uq_bind_quota_scope", "bind_quota", ["project_id", "cluster_id", "device_class"])
