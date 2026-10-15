import datetime as dt

import pytest

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from app.db import retention

from tests.conftest import fetch


NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_a_batch_deletes_only_old_events_the_reactor_has_read() -> None:
    sql = _sql(retention.prune_statement(NOW, 10))
    assert sql.startswith("DELETE FROM ops_event")
    assert "ops_event.occurred_at < " in sql
    assert (
        "NOT (EXISTS (SELECT * \nFROM ops_reactor_state \nWHERE ops_reactor_state.last_event_seq < ops_event.seq))"
        in sql
    )
    assert "ORDER BY ops_event.seq" in sql
    assert "LIMIT" in sql


def test_the_command_defaults_to_ninety_days() -> None:
    args = retention.parse([])
    assert (args.older_than_days, args.batch) == (90, 5000)


@pytest.mark.parametrize("argv", [["--older-than-days", "0"], ["--batch", "0"]])
def test_the_command_refuses_a_zero_window_or_batch(argv) -> None:
    with pytest.raises(SystemExit):
        retention.parse(argv)


async def test_a_zero_batch_is_refused() -> None:
    with pytest.raises(ValueError):
        await retention.prune_events(None, NOW, 0)


async def _event(sessions, uid: str, occurred_at: dt.datetime) -> int:
    async with sessions.begin() as db:
        result = await db.execute(
            text(
                "INSERT INTO ops_event (uid, type, subject_type, message, occurred_at)"
                " VALUES (:uid, 'node.joined', 'node', :uid, :at) RETURNING seq"
            ),
            {"uid": uid, "at": occurred_at},
        )
        return result.scalar_one()


@pytest.mark.database
async def test_pruning_keeps_recent_events_and_ones_the_reactor_has_not_read(sessions) -> None:
    old = NOW - dt.timedelta(days=100)
    read = [await _event(sessions, f"evt_read{i}", old) for i in range(3)]
    await _event(sessions, "evt_unread", old)
    await _event(sessions, "evt_recent", NOW)
    async with sessions.begin() as db:
        await db.execute(
            text("INSERT INTO ops_reactor_state (name, last_event_seq) VALUES ('default', :seq)"), {"seq": read[-1]}
        )

    deleted = await retention.prune_events(sessions, NOW - dt.timedelta(days=90), batch=2)

    assert deleted == 3
    assert [uid for (uid,) in await fetch(sessions, "SELECT uid FROM ops_event ORDER BY seq")] == [
        "evt_unread",
        "evt_recent",
    ]
