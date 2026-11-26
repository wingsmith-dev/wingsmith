"""artifacts get a handle and an owner scope

Revision ID: 3b7e5d1c9a42
Revises: 8d2f6b0e1a57
Create Date: 2026-11-26 10:00:00.000000

`slug` collides by design (one repo at two commits is two artifacts with one slug) and `name` is
display text, so nothing named an artifact exactly except its uid. `handle` is that name: unique,
DNS-subdomain shaped, and what a manifest's `metadata.name` will be.

`owner_project_id` scopes it: NULL is the public catalogue, set is private to one project. Handle
and digest are each unique within a scope (two partial unique indexes apiece), replacing the
global unique on digest. Every existing row is public.

Existing rows are backfilled oldest first, so the artifact registered first keeps the plain
handle and later ones get a digest suffix. The derivation is a frozen copy of
`app.modules.artifacts.handle`, because a migration must not change when the app does.
"""

import re
import hashlib

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "3b7e5d1c9a42"
down_revision: str | None = "8d2f6b0e1a57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base(slug: str, name: str) -> str:
    text = (slug or "").strip().lower() or (name or "").strip().lower()
    text = re.sub(r"[^a-z0-9.]+", "-", text)
    text = re.sub(r"[-.]{2,}", "-", text)
    return text[:200].strip("-.") or "artifact"


def _candidates(slug: str, name: str, digest: str) -> list[str]:
    plain = _base(slug, name)
    hashed = hashlib.sha256(digest.encode()).hexdigest()
    return [plain, f"{plain}-{hashed[:8]}", f"{plain}-{hashed[:16]}", f"{plain}-{hashed}"]


def upgrade() -> None:
    op.add_column("demand_artifact", sa.Column("handle", sa.String(length=253), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, slug, name, digest FROM demand_artifact ORDER BY seq")).all()
    taken: set[str] = set()
    for row in rows:
        handle = next(h for h in _candidates(row.slug, row.name, row.digest) if h not in taken)
        taken.add(handle)
        bind.execute(sa.text("UPDATE demand_artifact SET handle = :h WHERE id = :id"), {"h": handle, "id": row.id})
    op.alter_column("demand_artifact", "handle", nullable=False)
    op.add_column(
        "demand_artifact",
        sa.Column(
            "owner_project_id", sa.UUID(), sa.ForeignKey("bind_project.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    op.create_index("ix_demand_artifact_owner_project_id", "demand_artifact", ["owner_project_id"])
    op.drop_constraint("demand_artifact_digest_key", "demand_artifact", type_="unique")
    for column in ("handle", "digest"):
        op.create_index(
            f"uq_demand_artifact_public_{column}",
            "demand_artifact",
            [column],
            unique=True,
            postgresql_where=sa.text("owner_project_id IS NULL"),
        )
        op.create_index(
            f"uq_demand_artifact_private_{column}",
            "demand_artifact",
            ["owner_project_id", column],
            unique=True,
            postgresql_where=sa.text("owner_project_id IS NOT NULL"),
        )


def downgrade() -> None:
    private = op.get_bind().execute(sa.text("SELECT count(*) FROM demand_artifact WHERE owner_project_id IS NOT NULL"))
    if private.scalar():
        raise RuntimeError("project-private artifacts exist; the old schema has no scope to hold them")
    for column in ("handle", "digest"):
        op.drop_index(f"uq_demand_artifact_private_{column}", table_name="demand_artifact")
        op.drop_index(f"uq_demand_artifact_public_{column}", table_name="demand_artifact")
    op.create_unique_constraint("demand_artifact_digest_key", "demand_artifact", ["digest"])
    op.drop_index("ix_demand_artifact_owner_project_id", table_name="demand_artifact")
    op.drop_column("demand_artifact", "owner_project_id")
    op.drop_column("demand_artifact", "handle")
