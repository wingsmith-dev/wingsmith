"""seed the priority classes a schedulable workload cannot do without

Revision ID: d7a3e5f9c1b6
Revises: c6f2d49e8b5a
Create Date: 2026-08-20 18:40:00.000000

Every deployment carries a priority class — the column is not nullable, and that is
the design: preemption should be a policy change later rather than a migration across
every workload. Without a seeded default a fresh install could not create a
deployment at all: `POST /v1/deployments` and `.../promote` both fall back to the
class marked default, and with none the request failed with `no priority class was
given and none is marked default`.

Two classes, because one is not a priority system: `standard` is what everything gets
and is the default, `batch` is preemptible and preempts nothing. Values are 100 and 10,
leaving room either side for classes an operator adds later.

Seeded only into a table that is empty. An existing plane has made these decisions
already, and a migration that inserted a second default would change what every
deployment without an explicit class resolves to.
"""

import os
import time

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op


revision: str = "d7a3e5f9c1b6"
down_revision: Union[str, None] = "c6f2d49e8b5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEEDED = ("standard", "batch")

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode(value: int, length: int) -> str:
    out = [""] * length
    for i in range(length - 1, -1, -1):
        out[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def _priority_class_uid() -> str:
    """A `prc_` ULID, minted here so the migration does not import application code."""
    now = int(time.time() * 1000)
    return f"prc_{_encode(now, 10)}{_encode(int.from_bytes(os.urandom(10), 'big'), 16)}"


def upgrade() -> None:
    connection = op.get_bind()
    existing = connection.execute(sa.text("SELECT count(*) FROM bind_priority_class")).scalar_one()
    if existing:
        return
    connection.execute(
        sa.text(
            "INSERT INTO bind_priority_class"
            " (uid, name, value, preemptible, preempts, is_default, description)"
            " VALUES"
            " (:standard_uid, 'standard', 100, false, true, true,"
            "  'What a workload gets when nothing else is asked for.'),"
            " (:batch_uid, 'batch', 10, true, false, false,"
            "  'Runs on what is idle and yields to anything that is not.')"
        ),
        {"standard_uid": _priority_class_uid(), "batch_uid": _priority_class_uid()},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM bind_priority_class WHERE name = ANY(:names)"),
        {"names": list(SEEDED)},
    )
