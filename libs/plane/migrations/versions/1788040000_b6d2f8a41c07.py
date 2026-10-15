"""declared served model name on recipes and workloads

Revision ID: b6d2f8a41c07
Revises: f7b2d5e81a94
Create Date: 2026-09-01 10:00:00.000000

The model id a client sends used to be the recipe's name, verbatim, which put the fit
(`<model>-<quant>-tp8-<context>`) on the public API surface and renamed the model on
every re-fit. The new default is the artifact's lowercase Hugging Face slug; these two
columns are the explicit way to say otherwise, recipe first and workload over it.

Every existing recipe is backfilled with its current name, so clients already pointed
at that string keep working and the derived default applies only to recipes created
after this ran. Workloads are not backfilled: null there means defer to the recipe.
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
