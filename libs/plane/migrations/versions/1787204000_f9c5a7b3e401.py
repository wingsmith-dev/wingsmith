"""the reactor: alerts, the channels they are sent to, and the loop's own lease

Revision ID: f9c5a7b3e401
Revises: e8b4f6a2d3c7
Create Date: 2026-08-20 19:20:00.000000

Three tables for one gap. The plane recorded every fact needed to know that a
deployment had been down for seventeen hours — replica counts, cluster
reachability, an append-only event log — and had nothing that ever read them back
and said so. `ops_alert` is that reading, made durable so it can be acknowledged;
`ops_notification_channel` is where it gets sent; `ops_reactor_state` is the lease
and cursor that keep exactly one process doing either.

`ops_alert.subject_uid` is a string and deliberately not a foreign key. An alert
about a node routinely outlives the node — that is frequently the alert — and a
cascade would delete the record along with the thing it was about.

No backfill. Conditions are level-triggered and the first tick after this lands
evaluates the whole fleet from scratch, so anything currently broken opens an
alert within a tick without this migration inventing history. Events are drained
from `last_event_seq`, which starts at 0: the first tick walks the existing log in
batches and opens alerts for whatever in it still matches, which is the wanted
behaviour on an install that has been quietly accumulating problems.
"""

from typing import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f9c5a7b3e401"
down_revision: Union[str, None] = "e8b4f6a2d3c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ops_alert",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("dedupe_key", sa.String(length=256), nullable=False),
        sa.Column("rule", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), server_default="condition", nullable=False),
        sa.Column("severity", sa.String(length=16), server_default="warning", nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("cluster_id", sa.UUID(), nullable=True),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_uid", sa.String(length=64), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurrences", sa.Integer(), server_default="1", nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=128), nullable=True),
        sa.Column("acknowledged_note", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_state", sa.String(length=16), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notify_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["cluster_id"], ["supply_cluster.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_ops_alert_rule", "ops_alert", ["rule"])
    op.create_index("ix_ops_alert_severity", "ops_alert", ["severity"])
    op.create_index("ix_ops_alert_state", "ops_alert", ["state"])
    op.create_index("ix_ops_alert_state_seq", "ops_alert", ["state", "seq"])
    op.create_index("ix_ops_alert_cluster", "ops_alert", ["cluster_id"])

    op.create_table(
        "ops_notification_channel",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("uid", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("min_severity", sa.String(length=16), server_default="warning", nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("uid"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "ops_reactor_state",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seq", sa.Integer(), sa.Identity(start=1000, cycle=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("holder", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("ops_reactor_state")
    op.drop_table("ops_notification_channel")
    op.drop_index("ix_ops_alert_cluster", table_name="ops_alert")
    op.drop_index("ix_ops_alert_state_seq", table_name="ops_alert")
    op.drop_index("ix_ops_alert_state", table_name="ops_alert")
    op.drop_index("ix_ops_alert_severity", table_name="ops_alert")
    op.drop_index("ix_ops_alert_rule", table_name="ops_alert")
    op.drop_table("ops_alert")
