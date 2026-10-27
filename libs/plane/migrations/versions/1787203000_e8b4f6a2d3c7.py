"""mark a default priority class on a plane that has classes but no default

Revision ID: e8b4f6a2d3c7
Revises: d7a3e5f9c1b6
Create Date: 2026-08-20 19:20:00.000000

`d7a3e5f9c1b6` seeded `standard` and `batch` only into an empty table, which is right:
a plane that has made these decisions should keep them, and a second default would
change what every deployment without an explicit class resolves to. It also left one
state untreated: a plane holding classes where none is marked default. Somebody creates
one class through the console, `is_default` defaults to false, the seed skips a table
that is no longer empty, and every write that does not name a class still fails with
`no priority class was given and none is marked default`.

The highest value wins, because value is the ordering the class exists to express and it
is unique in the database, so the choice is deterministic instead of whatever the
planner returned that day. Nothing is inserted and nothing already marked is touched.
"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = "e8b4f6a2d3c7"
down_revision: Union[str, None] = "d7a3e5f9c1b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE bind_priority_class SET is_default = true"
            " WHERE id = ("
            "   SELECT id FROM bind_priority_class"
            "   WHERE NOT EXISTS (SELECT 1 FROM bind_priority_class WHERE is_default)"
            "   ORDER BY value DESC LIMIT 1"
            " )"
        )
    )


def downgrade() -> None:
    """Nothing. Which class was marked here is indistinguishable from one marked by hand."""
