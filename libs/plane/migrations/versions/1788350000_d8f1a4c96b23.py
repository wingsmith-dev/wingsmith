"""frontend image on the recipe

Revision ID: d8f1a4c96b23
Revises: 6c29bbd8d370
Create Date: 2026-09-02 11:53:20.000000

A cluster ran one Dynamo frontend for every model, which made the frontend image a
fleet-wide lowest common denominator: one binary had to tokenize and register every
model on the cluster. A model whose tokenizer only a newer frontend build understands
is then healthy and unreachable unless the image every other model is served through
moves too.

So the frontend becomes one per model, and this column is what a model says about the
one it gets. It is a second image and not a reuse of `engine_image` because the two are
separately versioned binaries.

Nullable, no backfill, no derivation. Null means the cluster decides: the operator
knows what that cluster runs its frontends on and the plane does not. Every existing
row is null, which is the previous behaviour.
"""

from typing import Union
from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "d8f1a4c96b23"
down_revision: Union[str, None] = "6c29bbd8d370"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("demand_recipe", sa.Column("frontend_image", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("demand_recipe", "frontend_image")
