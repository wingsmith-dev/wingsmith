"""The reactor against a real Postgres.

One spot GPU node in one cluster. The node goes away and the workload on it stops
serving; each test here checks that the plane says so.
"""

import uuid
import datetime as dt

from types import SimpleNamespace

import pytest

from sqlalchemy import text

from app.core import ids
from app.reactor import loop
from app.reactor import alerts as lifecycle
from app.reactor.loop import tick
from app.reactor.conditions import evaluate
from app.modules.alerts.schemas import AlertAcknowledgeRequest
from app.modules.alerts.services import AlertService

from tests.conftest import fetch


pytestmark = pytest.mark.database

INSTANCE = "reactor-test"


@pytest.fixture(autouse=True)
def fresh_reactor_memory(monkeypatch):
    """What the lease holder remembers between ticks, started empty for every test."""
    monkeypatch.setattr(loop, "LATE", loop.LateCommits())
    monkeypatch.setattr(loop, "LEDGER", loop.DeliveryLedger())
    # The repair pass re-places workloads, and other suites leave workloads behind; only the
    # tests about it run it.
    monkeypatch.setattr(loop.reconcile, "CADENCE", SimpleNamespace(due=lambda: False))


async def connect(sessions, cluster: uuid.UUID, reachable: bool = True) -> None:
    """Marks the cluster active and its agent connected, the way enrolment and `hello` would.

    The phase matters as much as the reachability. Every condition is scoped to
    clusters we expect to be carrying work, so a `pending` cluster — which is what
    the shared fixture seeds — is deliberately silent.
    """
    async with sessions.begin() as db:
        await db.execute(text("UPDATE supply_cluster SET phase = 'active' WHERE id = :cluster"), {"cluster": cluster})
        await db.execute(
            text(
                "INSERT INTO supply_cluster_sync_state (cluster_id, reachable, connected_at, last_seen_at)"
                " VALUES (:cluster, :reachable, now(), now())"
                " ON CONFLICT (cluster_id) DO UPDATE SET reachable = :reachable"
            ),
            {"cluster": cluster, "reachable": reachable},
        )


async def seed_node(sessions, cluster: uuid.UUID, devices: int = 1) -> None:
    async with sessions.begin() as db:
        node = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO supply_node (id, uid, cluster_id, name, condition)"
                " VALUES (:id, :uid, :cluster, 'gpu-1', 'ready')"
            ),
            {"id": node, "uid": ids.new_uid(ids.NODE), "cluster": cluster},
        )
        for index in range(devices):
            await db.execute(
                text(
                    "INSERT INTO supply_device"
                    " (uid, cluster_id, node_id, device_uuid, device_index, device_class)"
                    " VALUES (:uid, :cluster, :node, :device_uuid, :index, 'H100')"
                ),
                {
                    "uid": ids.new_uid(ids.DEVICE),
                    "cluster": cluster,
                    "node": node,
                    "device_uuid": f"GPU-{index}",
                    "index": index,
                },
            )


async def seed_workload(sessions, cluster: uuid.UUID, ready: int, desired: int = 1) -> str:
    uid = ids.new_uid(ids.DISCOVERED_WORKLOAD)
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO demand_discovered_workload"
                " (uid, cluster_id, namespace, name, kind, replicas_desired, replicas_ready)"
                " VALUES (:uid, :cluster, 'serving', 'llama', 'Deployment', :desired, :ready)"
            ),
            {"uid": uid, "cluster": cluster, "desired": desired, "ready": ready},
        )
    return uid


async def alert_rows(sessions, rule: str | None = None) -> list:
    where = " WHERE rule = :rule" if rule else ""
    return await fetch(
        sessions,
        f"SELECT rule, state, severity, title, occurrences, dedupe_key FROM ops_alert{where} ORDER BY seq",
        {"rule": rule} if rule else {},
    )


async def test_an_unimported_workload_with_nothing_running_is_still_seen(sessions, cluster):
    """The exact gap: no declared deployment, so nothing that reasons about intent could see it.

    The sighting carries the cluster's own desired and ready counts, and that is
    enough to know the workload is broken without anyone having promoted it.
    """
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    await seed_workload(sessions, cluster, ready=0)

    async with sessions() as db:
        intents = await evaluate(db)

    stalled = [i for i in intents if i.rule == "workload.stalled"]
    assert len(stalled) == 1
    assert stalled[0].details == {"replicas_ready": 0, "replicas_desired": 1, "promoted": False}


async def test_a_healthy_workload_raises_nothing(sessions, cluster):
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    await seed_workload(sessions, cluster, ready=1)

    async with sessions() as db:
        intents = await evaluate(db)
    assert [i for i in intents if i.rule == "workload.stalled"] == []


async def test_a_connected_cluster_with_no_devices_is_an_alert(sessions, cluster):
    """The spot node going away leaves reachability green and capacity at zero."""
    await connect(sessions, cluster)
    async with sessions() as db:
        intents = await evaluate(db)
    empty = [i for i in intents if i.rule == "cluster.no_capacity"]
    assert len(empty) == 1
    assert empty[0].details == {"nodes": 0, "devices": 0}


async def test_an_unreachable_cluster_is_reported_once_not_twice(sessions, cluster):
    """`cluster.no_capacity` is scoped to reachable clusters so one outage is one alert."""
    await connect(sessions, cluster, reachable=False)
    async with sessions() as db:
        intents = await evaluate(db)
    assert [i.rule for i in intents] == ["cluster.unreachable"]


async def test_a_condition_waits_before_it_fires_and_then_fires(sessions, cluster):
    """Pending is silent until the `for` window has passed, so one tick is not an alert."""
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    await seed_workload(sessions, cluster, ready=0)

    assert await tick(sessions, INSTANCE)
    rows = await alert_rows(sessions, "workload.stalled")
    assert [r.state for r in rows] == [lifecycle.PENDING]

    async with sessions.begin() as db:
        await db.execute(text("UPDATE ops_alert SET first_seen_at = now() - interval '17 hours'"))

    assert await tick(sessions, INSTANCE)
    rows = await alert_rows(sessions, "workload.stalled")
    assert [r.state for r in rows] == [lifecycle.FIRING]


async def test_a_condition_that_goes_away_resolves_itself(sessions, cluster):
    """Level-triggered: absence from a full re-evaluation is what closes a condition alert."""
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    uid = await seed_workload(sessions, cluster, ready=0)
    await tick(sessions, INSTANCE)

    async with sessions.begin() as db:
        await db.execute(
            text("UPDATE demand_discovered_workload SET replicas_ready = 1 WHERE uid = :uid"), {"uid": uid}
        )
    await tick(sessions, INSTANCE)

    rows = await alert_rows(sessions, "workload.stalled")
    assert [r.state for r in rows] == [lifecycle.RESOLVED]


async def test_a_recurrence_reopens_the_same_row_rather_than_starting_a_second_history(sessions, cluster):
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    uid = await seed_workload(sessions, cluster, ready=0)
    await tick(sessions, INSTANCE)
    async with sessions.begin() as db:
        await db.execute(
            text("UPDATE demand_discovered_workload SET replicas_ready = 1 WHERE uid = :uid"), {"uid": uid}
        )
    await tick(sessions, INSTANCE)
    async with sessions.begin() as db:
        await db.execute(
            text("UPDATE demand_discovered_workload SET replicas_ready = 0 WHERE uid = :uid"), {"uid": uid}
        )
    await tick(sessions, INSTANCE)

    rows = await alert_rows(sessions, "workload.stalled")
    assert len(rows) == 1
    assert rows[0].occurrences == 2
    assert rows[0].state == lifecycle.PENDING


async def test_ticking_repeatedly_does_not_multiply_alerts(sessions, cluster):
    """Idempotence on `dedupe_key`, which is the difference between an alert and a firehose."""
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    await seed_workload(sessions, cluster, ready=0)
    for _ in range(4):
        await tick(sessions, INSTANCE)
    assert len(await alert_rows(sessions, "workload.stalled")) == 1


async def test_only_one_process_holds_the_lease(sessions, cluster):
    """Two hubs both evaluating is harmless; two hubs both messaging Slack is not."""
    await connect(sessions, cluster)
    assert await tick(sessions, INSTANCE)
    assert not await tick(sessions, "another-hub")


async def test_an_expired_lease_is_taken_over(sessions, cluster):
    await connect(sessions, cluster)
    await tick(sessions, INSTANCE)
    async with sessions.begin() as db:
        await db.execute(text("UPDATE ops_reactor_state SET lease_expires_at = now() - interval '1 hour'"))
    assert await tick(sessions, "another-hub")


async def test_an_error_event_becomes_an_alert_and_the_cursor_moves_past_it(sessions, cluster):
    """Edge-triggered, from the log ingestion already writes."""
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ops_event (uid, cluster_id, type, severity, subject_type, message, occurred_at)"
                " VALUES (:uid, :cluster, 'node.left', 'warning', 'node', 'node gpu-1 left', now())"
            ),
            {"uid": ids.new_uid(ids.EVENT), "cluster": cluster},
        )
    await tick(sessions, INSTANCE)

    rows = await alert_rows(sessions, "node.left")
    assert len(rows) == 1
    assert rows[0].severity == "error"

    cursor = await fetch(sessions, "SELECT last_event_seq FROM ops_reactor_state")
    assert cursor[0][0] > 0

    await tick(sessions, INSTANCE)
    assert len(await alert_rows(sessions, "node.left")) == 1


async def test_an_acknowledgement_survives_the_next_evaluation(sessions, cluster):
    """The one column the reactor may not touch. Without this, acknowledging anything is theatre."""
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    await seed_workload(sessions, cluster, ready=0)
    await tick(sessions, INSTANCE)

    async with sessions() as db:
        svc = AlertService(db, actor="operator")
        listed = await svc.list(rule="workload.stalled")
        acked = await svc.acknowledge(listed.items[0].id, AlertAcknowledgeRequest(note="known, chasing capacity"))
    assert acked.state == lifecycle.ACKNOWLEDGED
    assert acked.acknowledged_by == "operator"

    await tick(sessions, INSTANCE)
    rows = await alert_rows(sessions, "workload.stalled")
    assert rows[0].state == lifecycle.ACKNOWLEDGED


async def test_an_acknowledged_condition_still_resolves_when_it_recovers(sessions, cluster):
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    uid = await seed_workload(sessions, cluster, ready=0)
    await tick(sessions, INSTANCE)
    async with sessions() as db:
        svc = AlertService(db, actor="operator")
        listed = await svc.list(rule="workload.stalled")
        await svc.acknowledge(listed.items[0].id, AlertAcknowledgeRequest())
    async with sessions.begin() as db:
        await db.execute(
            text("UPDATE demand_discovered_workload SET replicas_ready = 1 WHERE uid = :uid"), {"uid": uid}
        )
    await tick(sessions, INSTANCE)
    rows = await alert_rows(sessions, "workload.stalled")
    assert rows[0].state == lifecycle.RESOLVED


async def test_nothing_is_sent_while_an_alert_is_only_pending(sessions, cluster, monkeypatch):
    """Pending is silent by construction, and that is what makes the rest of it readable."""
    sent = []

    async def record(channel, message, timeout):
        sent.append(message)

    monkeypatch.setattr(loop.notify, "deliver", record)
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ops_notification_channel (uid, name, kind, min_severity, config)"
                " VALUES (:uid, 'ops', 'slack', 'warning', '{\"webhook_url\": \"https://example.invalid/x\"}')"
            ),
            {"uid": ids.new_uid(ids.NOTIFICATION_CHANNEL)},
        )
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    await seed_workload(sessions, cluster, ready=0)

    await tick(sessions, INSTANCE)
    assert sent == []

    async with sessions.begin() as db:
        await db.execute(text("UPDATE ops_alert SET first_seen_at = now() - interval '17 hours'"))
    await tick(sessions, INSTANCE)
    assert [m.state for m in sent] == [lifecycle.FIRING]

    await tick(sessions, INSTANCE)
    assert len(sent) == 1


async def test_a_failed_delivery_is_recorded_and_retried(sessions, cluster, monkeypatch):
    """The notified marker only advances on success, so a broken webhook is not silent forever."""
    attempts = []

    async def fail(channel, message, timeout):
        attempts.append(message)
        raise loop.notify.DeliveryFailed("slack answered 404")

    monkeypatch.setattr(loop.notify, "deliver", fail)
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ops_notification_channel (uid, name, kind, min_severity, config)"
                " VALUES (:uid, 'ops', 'slack', 'warning', '{\"webhook_url\": \"https://example.invalid/x\"}')"
            ),
            {"uid": ids.new_uid(ids.NOTIFICATION_CHANNEL)},
        )
    await connect(sessions, cluster, reachable=False)
    async with sessions.begin() as db:
        await db.execute(text("UPDATE supply_cluster_sync_state SET last_seen_at = now() - interval '2 days'"))

    await tick(sessions, INSTANCE)
    async with sessions.begin() as db:
        await db.execute(text("UPDATE ops_alert SET first_seen_at = now() - interval '1 hour'"))
    await tick(sessions, INSTANCE)
    await tick(sessions, INSTANCE)

    assert len(attempts) == 2
    rows = await fetch(sessions, "SELECT notify_error, notified_state FROM ops_alert")
    assert rows[0][0] == "ops: slack answered 404"
    assert rows[0][1] is None


async def test_one_failing_channel_does_not_make_the_healthy_one_hear_it_twice(sessions, cluster, monkeypatch):
    """A failure used to leave the whole alert due, and every channel was sent it again next tick."""
    sent: list[str] = []

    async def deliver(channel, message, timeout):
        if channel.name == "broken":
            raise loop.notify.DeliveryFailed("slack answered 404")
        sent.append(channel.name)

    monkeypatch.setattr(loop.notify, "deliver", deliver)
    async with sessions.begin() as db:
        for name in ("healthy", "broken"):
            await db.execute(
                text(
                    "INSERT INTO ops_notification_channel (uid, name, kind, min_severity, config)"
                    " VALUES (:uid, :name, 'slack', 'warning', '{\"webhook_url\": \"https://example.invalid/x\"}')"
                ),
                {"uid": ids.new_uid(ids.NOTIFICATION_CHANNEL), "name": name},
            )
    await connect(sessions, cluster, reachable=False)
    async with sessions.begin() as db:
        await db.execute(text("UPDATE supply_cluster_sync_state SET last_seen_at = now() - interval '2 days'"))

    await tick(sessions, INSTANCE)
    async with sessions.begin() as db:
        await db.execute(text("UPDATE ops_alert SET first_seen_at = now() - interval '1 hour'"))
    for _ in range(4):
        await tick(sessions, INSTANCE)

    assert sent == ["healthy"]


async def test_an_event_committed_after_the_cursor_passed_its_seq_still_opens_an_alert(sessions, cluster):
    """`seq` is taken at insert and seen at commit, and a slow transaction commits out of order."""

    async def insert(seq: int, message: str) -> None:
        async with sessions.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO ops_event (seq, uid, cluster_id, type, severity, subject_type, message, occurred_at)"
                    " VALUES (:seq, :uid, :cluster, 'replica.oom', 'error', 'replica', :message, now())"
                ),
                {"seq": seq, "uid": ids.new_uid(ids.EVENT), "cluster": cluster, "message": message},
            )

    await insert(900001, "first")
    await insert(900003, "third")
    await tick(sessions, INSTANCE)
    assert len(await alert_rows(sessions, "replica.oom")) == 2

    await insert(900002, "committed late")
    await tick(sessions, INSTANCE)

    assert len(await alert_rows(sessions, "replica.oom")) == 3


async def test_the_evaluator_never_writes_declared_or_observed_state(sessions, cluster):
    """Rule 1 holds here too: the reactor derives, and derived state is its own table."""
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    uid = await seed_workload(sessions, cluster, ready=0)
    before = await fetch(
        sessions,
        "SELECT replicas_ready, replicas_desired FROM demand_discovered_workload WHERE uid = :uid",
        {"uid": uid},
    )
    await tick(sessions, INSTANCE)
    after = await fetch(
        sessions,
        "SELECT replicas_ready, replicas_desired FROM demand_discovered_workload WHERE uid = :uid",
        {"uid": uid},
    )
    assert before == after


async def test_alerts_outlive_the_thing_they_are_about(sessions, cluster):
    """`subject_uid` is a string, not a foreign key — the node going away is the alert."""
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    uid = await seed_workload(sessions, cluster, ready=0)
    await tick(sessions, INSTANCE)
    async with sessions.begin() as db:
        await db.execute(text("DELETE FROM demand_discovered_workload WHERE uid = :uid"), {"uid": uid})
    rows = await alert_rows(sessions, "workload.stalled")
    assert rows[0].dedupe_key.endswith(uid)


async def test_a_reactor_that_was_down_works_through_the_backlog(sessions, cluster):
    """The cursor starts at zero, so an install that has been quietly accumulating gets read."""
    async with sessions.begin() as db:
        for index in range(3):
            await db.execute(
                text(
                    "INSERT INTO ops_event (uid, cluster_id, type, severity, subject_type, message, occurred_at)"
                    " VALUES (:uid, :cluster, 'replica.oom', 'error', 'replica', :message, now())"
                ),
                {"uid": ids.new_uid(ids.EVENT), "cluster": cluster, "message": f"killed {index}"},
            )
    await tick(sessions, INSTANCE)
    assert len(await alert_rows(sessions, "replica.oom")) == 3


async def test_a_label_on_the_deployment_mutes_it_without_a_deploy(sessions, cluster):
    """The knob the console can turn: a label edit, not a code change.

    Muting removes the condition from the evaluated set, and absence is what
    resolves a condition alert — so an alert already firing goes quiet on the next
    tick rather than lingering until someone closes it by hand.
    """
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    uid = await seed_workload(sessions, cluster, ready=0)
    await tick(sessions, INSTANCE)
    assert len(await alert_rows(sessions, "workload.stalled")) == 1

    async with sessions.begin() as db:
        await db.execute(
            text('UPDATE demand_discovered_workload SET labels = \'{"hangar.io/alert": "off"}\' WHERE uid = :uid'),
            {"uid": uid},
        )
    await tick(sessions, INSTANCE)

    rows = await alert_rows(sessions, "workload.stalled")
    assert [r.state for r in rows] == [lifecycle.RESOLVED]


async def test_a_label_can_lengthen_the_window_before_anything_fires(sessions, cluster):
    """`hangar.io/alert.for` moves the line between "reconciling" and "broken", per object."""
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    uid = await seed_workload(sessions, cluster, ready=0)
    async with sessions.begin() as db:
        await db.execute(
            text('UPDATE demand_discovered_workload SET labels = \'{"hangar.io/alert.for": "6h"}\' WHERE uid = :uid'),
            {"uid": uid},
        )
    await tick(sessions, INSTANCE)
    async with sessions.begin() as db:
        await db.execute(text("UPDATE ops_alert SET first_seen_at = now() - interval '2 hours'"))

    await tick(sessions, INSTANCE)
    rows = await alert_rows(sessions, "workload.stalled")
    assert [r.state for r in rows] == [lifecycle.PENDING], "two hours is inside the six the label asked for"

    async with sessions.begin() as db:
        await db.execute(text("UPDATE ops_alert SET first_seen_at = now() - interval '7 hours'"))
    await tick(sessions, INSTANCE)
    assert [r.state for r in await alert_rows(sessions, "workload.stalled")] == [lifecycle.FIRING]


async def test_a_label_can_change_the_severity_a_channel_routes_on(sessions, cluster):
    await connect(sessions, cluster)
    await seed_node(sessions, cluster)
    uid = await seed_workload(sessions, cluster, ready=0)
    async with sessions.begin() as db:
        await db.execute(
            text(
                "UPDATE demand_discovered_workload"
                ' SET labels = \'{"hangar.io/alert.severity": "warning"}\' WHERE uid = :uid'
            ),
            {"uid": uid},
        )
    await tick(sessions, INSTANCE)
    rows = await alert_rows(sessions, "workload.stalled")
    assert rows[0].severity == "warning"


async def test_an_operation_past_its_deadline_is_timed_out_and_stays_that_way(sessions, cluster):
    """Nothing closed an operation whose cluster never answered; it stayed `running` for ever."""
    from app.hub.sink import SinkFrame
    from app.hub.state import new_id
    from app.ingestion.sink import DatabaseSink

    from tests.test_ingestion_db import payload

    operation = "opr_01j9x2kkkkkkkkkkkkkkkkkkkz"
    async with sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ops_operation (uid, cluster_id, type, target_type, target_id, requested_by, phase,"
                " deadline_at) VALUES (:uid, :cluster, 'drain', 'node', gen_random_uuid(), 'staff', 'running',"
                " now() - interval '1 minute')"
            ),
            {"uid": operation, "cluster": cluster},
        )

    await loop._repair(sessions, dt.datetime.now(dt.UTC))

    rows = await fetch(
        sessions, "SELECT phase, finished_at IS NOT NULL FROM ops_operation WHERE uid = :u", {"u": operation}
    )
    assert rows == [("timed_out", True)]

    await DatabaseSink(sessions).record_operation(
        SinkFrame(
            cluster_id=cluster_uid(),
            type="operation.observed",
            payload=payload("operation.observed", operation_id=operation, phase="running"),
            id=new_id(),
        )
    )
    rows = await fetch(sessions, "SELECT phase FROM ops_operation WHERE uid = :u", {"u": operation})
    assert rows == [("timed_out",)], "a late report does not reopen an operation that ended"


def cluster_uid() -> str:
    from tests.conftest import CLUSTER

    return CLUSTER
