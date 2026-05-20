"""declared served model name on recipes and workloads

Revision ID: b6d2f8a41c07
Revises: f7b2d5e81a94
Create Date: 2026-09-01 10:00:00.000000

The model id a client sends used to be the recipe's name, verbatim. That put the
fit on the public API surface — `kimi-k3-mxfp4-tp8-1024k` — and made re-fitting
the same weights at a different tensor degree rename the model every caller codes
against. The new default is the artifact's lowercase Hugging Face slug, which is a
fact about the weights and survives a re-fit; these two columns are the explicit
way to say otherwise, recipe first and workload over it.

The backfill is the whole point of this migration. Every recipe that exists today
is serving under its own name and there are clients pointed at that string, so
leaving the column null would let the new default take over on the next reconcile
and break all of them at once — silently, because a wrong `model` is a 404 from
the frontend and not an error anywhere in the plane. Writing the current name into
the override makes the upgrade a no-op on the wire: existing recipes keep serving
exactly what they serve now, and the derived default applies only to recipes
created after this ran.

Workloads are not backfilled. They had no name of their own to preserve, so null
there means what it will always mean — defer to the recipe.

"""

from typing import Union
from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "b6d2f8a41c07"
down_revision: Union[str, None] = "f7b2d5e81a94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("demand_recipe", sa.Column("served_model_name", sa.String(length=255), nullable=True))
    op.add_column("demand_workload", sa.Column("served_model_name", sa.String(length=255), nullable=True))
    op.execute("UPDATE demand_recipe SET served_model_name = name WHERE served_model_name IS NULL")


def downgrade() -> None:
    op.drop_column("demand_workload", "served_model_name")
    op.drop_column("demand_recipe", "served_model_name")
