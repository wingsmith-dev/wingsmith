"""prefix caching is on unless a recipe says otherwise

Revision ID: f7b2d5e81a94
Revises: e4f1a7c92d18
Create Date: 2026-08-31 23:58:00.000000

`prefix_cache_enabled` defaulted false, which was the default a boolean gets when
nobody picks one rather than a position anybody took. vLLM enables prefix caching
itself, so a recipe that never mentioned the field rendered
`--no-enable-prefix-caching` and turned off the mechanism the rest of the serving
path assumes: `PYTHONHASHSEED=0` on every worker exists only so KV-aware routing
can find the worker already holding a prefix, and the gateway reports
`cached_input` tokens that such a recipe could never produce.

Existing rows are backfilled rather than left alone. False could not be told apart
from unset — there was no third state — so leaving them would keep every recipe
that exists on a default nobody chose, which is the thing this migration is for.
A recipe that wants the cache off now says so, and `downgrade` deliberately does
not undo the backfill: by then some of those rows are a real choice.

"""

from typing import Union
from typing import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "f7b2d5e81a94"
down_revision: Union[str, None] = "e4f1a7c92d18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("demand_recipe", "prefix_cache_enabled", server_default="true")
    op.execute(sa.text("UPDATE demand_recipe SET prefix_cache_enabled = true WHERE prefix_cache_enabled = false"))


def downgrade() -> None:
    op.alter_column("demand_recipe", "prefix_cache_enabled", server_default="false")
