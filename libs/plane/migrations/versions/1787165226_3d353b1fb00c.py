"""public ids, labels, and operation idempotency

Revision ID: 3d353b1fb00c
Revises: 272245edb7d9
Create Date: 2026-08-19 15:47:06.472650

Five objects reached the API as bare UUIDs because their tables carried no `uid`.
This adds one to each and backfills the rows that already exist before the column
is made `NOT NULL`, so an id is minted for history rather than left null in a
column that forbids nulls.

Three constraints come with it: priority values become unique, because a tie in
`value` makes eviction order undefined and a service-level check loses the race
between two concurrent writers; `ops_operation.idempotency_key` is promoted out
of `payload` and given a partial unique index, because only the index can stop
two concurrent retries of one drain becoming two drains; and `labels` lands on
the three demand tables that `libs/packages/contract/API.md` §6 says are filterable by label.
"""

import os
import time

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = '3d353b1fb00c'
down_revision: Union[str, None] = '272245edb7d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UID_TABLES = (
    ("bind_tenant", "ten_"),
    ("bind_priority_class", "prc_"),
    ("bind_policy", "plc_"),
    ("bind_allocation", "alc_"),
    ("demand_workload", "wkl_"),
)

LABEL_TABLES = ("demand_recipe", "demand_deployment", "demand_endpoint")

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode(value: int, length: int) -> str:
    out = [""] * length
    for i in range(length - 1, -1, -1):
        out[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def _mint(prefix: str, counter: int) -> str:
    """Mints one backfill id in the same shape `app.core.ids.new_uid` produces.

    Spelled out here rather than imported: a migration is a record of what was
    run against a database on a given day, and importing application code would
    let a later edit to that code change what this revision means.
    """
    stamp = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big") ^ counter
    return f"{prefix}{_encode(stamp, 10)}{_encode(rand, 16)}"


def _backfill_uids(table: str, prefix: str) -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text(f"SELECT id FROM {table} ORDER BY seq")).fetchall()
    for counter, row in enumerate(rows):
        connection.execute(
            sa.text(f"UPDATE {table} SET uid = :uid WHERE id = :id"),
            {"uid": _mint(prefix, counter), "id": row[0]},
        )


def upgrade() -> None:
    for table, prefix in UID_TABLES:
        op.add_column(table, sa.Column("uid", sa.String(length=40), nullable=True))
        _backfill_uids(table, prefix)
        op.alter_column(table, "uid", nullable=False)
        op.create_unique_constraint(f"{table}_uid_key", table, ["uid"])

    op.drop_index(op.f("ix_bind_priority_class_value"), table_name="bind_priority_class")
    op.create_unique_constraint("uq_bind_priority_class_value", "bind_priority_class", ["value"])

    for table in LABEL_TABLES:
        op.add_column(
            table,
            sa.Column(
                "labels",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default="{}",
                nullable=False,
            ),
        )

    op.add_column("ops_operation", sa.Column("idempotency_key", sa.String(length=255), nullable=True))
    op.add_column("ops_operation", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE ops_operation
        SET idempotency_key = payload->>'idempotency_key'
        WHERE id IN (
            SELECT DISTINCT ON (payload->>'idempotency_key') id
            FROM ops_operation
            WHERE payload->>'idempotency_key' IS NOT NULL
            ORDER BY payload->>'idempotency_key', seq ASC
        )
        """
    )
    op.create_index(
        "uq_ops_operation_idempotency_key",
        "ops_operation",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_ops_operation_idempotency_key",
        table_name="ops_operation",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_column("ops_operation", "cancel_requested_at")
    op.drop_column("ops_operation", "idempotency_key")

    for table in LABEL_TABLES:
        op.drop_column(table, "labels")

    op.drop_constraint("uq_bind_priority_class_value", "bind_priority_class", type_="unique")
    op.create_index(op.f("ix_bind_priority_class_value"), "bind_priority_class", ["value"], unique=False)

    for table, _ in reversed(UID_TABLES):
        op.drop_constraint(f"{table}_uid_key", table, type_="unique")
        op.drop_column(table, "uid")
