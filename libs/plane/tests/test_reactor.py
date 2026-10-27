"""The reactor's judgement, exercised with no database.

Everything here is a pure function by design: `app.reactor.rules` decides what is
worth alerting on and `app.reactor.notify` decides what that looks like in Slack,
and neither needs a session to be wrong in an interesting way.
"""

import asyncio
import datetime as dt

from app.reactor import rules
from app.reactor import notify
from app.reactor import tuning
from app.reactor.alerts import FIRING
from app.reactor.alerts import PENDING
from app.reactor.alerts import RESOLVED
from app.reactor.alerts import should_send
from app.reactor.conditions import overdue


class FakeAlert:
    """Enough of an `Alert` for the pure helpers. Intentionally not the model."""

    def __init__(self, state: str, notified_state: str | None, severity: str = "error") -> None:
        self.state = state
        self.notified_state = notified_state
        self.severity = severity


def test_severity_floor_orders_and_defaults():
    assert rules.at_least(rules.ERROR, rules.WARNING)
    assert not rules.at_least(rules.INFO, rules.WARNING)
    assert rules.at_least(rules.CRITICAL, rules.CRITICAL)
    assert rules.at_least("something-new", rules.WARNING)


def test_named_event_alerts_regardless_of_the_severity_it_arrived_with():
    """`node.left` at `warning` still opens an alert: that override is the point of the table."""
    intent = rules.event_intent(
        event_type="node.left",
        severity=rules.WARNING,
        subject_type="node",
        message="node gpu-1 left the cluster",
        details={"reason": "preempted"},
        event_uid="evt_1",
        cluster_id=None,
        cluster_name="us",
    )
    assert intent is not None
    assert intent.severity == rules.ERROR
    assert intent.source == rules.EVENT
    assert intent.for_seconds == 0


def test_unremarkable_events_are_dropped_and_errors_are_not():
    common = {"subject_type": "node", "message": "m", "details": {}, "cluster_id": None, "cluster_name": None}
    assert rules.event_intent(event_type="node.joined", severity=rules.INFO, event_uid="evt_2", **common) is None
    assert rules.event_intent(event_type="thing.exploded", severity=rules.ERROR, event_uid="evt_3", **common)


def test_event_alerts_key_on_the_event_so_two_occurrences_stay_two_alerts():
    """Conditions collapse onto one row; events must not, or the second OOM hides behind the first."""
    common = {
        "event_type": "replica.oom",
        "severity": rules.ERROR,
        "subject_type": "replica",
        "message": "killed",
        "details": {},
        "cluster_id": None,
        "cluster_name": None,
    }
    first = rules.event_intent(event_uid="evt_a", **common)
    second = rules.event_intent(event_uid="evt_b", **common)
    assert first.dedupe_key != second.dedupe_key


def test_condition_keys_are_stable_across_evaluations():
    """The same ongoing problem must key identically however often it is evaluated."""
    args = (None, "us", "dep_1", "llama", 0, 2)
    assert rules.deployment_behind(*args).dedupe_key == rules.deployment_behind(*args).dedupe_key
    assert (
        rules.deployment_behind(*args).dedupe_key != rules.deployment_behind(None, "us", "dep_2", "x", 0, 2).dedupe_key
    )


def test_a_deployment_with_nothing_ready_is_worse_than_one_merely_behind():
    assert rules.deployment_behind(None, "us", "dep_1", "llama", 0, 2).severity == rules.ERROR
    assert rules.deployment_behind(None, "us", "dep_1", "llama", 1, 2).severity == rules.WARNING


def test_stalled_workload_says_whether_anyone_imported_it():
    """The founding scenario: nobody had promoted it, and that belongs in the message."""
    intent = rules.workload_stalled(None, "us", "dsw_1", "serving/llama", 0, 1, promoted=False)
    assert "not imported" in intent.title
    assert intent.details["promoted"] is False
    assert rules.workload_stalled(None, "us", "dsw_1", "serving/llama", 0, 1, promoted=True).title.endswith("ready")


def test_pending_becomes_overdue_only_after_the_window():
    start = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC)
    assert not overdue(start, start + dt.timedelta(minutes=14), 900)
    assert overdue(start, start + dt.timedelta(minutes=15), 900)
    assert overdue(start, start + dt.timedelta(hours=17), 900)


def test_recovery_is_only_announced_to_people_who_heard_the_alarm():
    assert should_send(FakeAlert(FIRING, None))
    assert should_send(FakeAlert(RESOLVED, FIRING))
    assert not should_send(FakeAlert(RESOLVED, None))
    assert not should_send(FakeAlert(RESOLVED, PENDING))


def test_slack_payload_carries_a_text_fallback_for_the_push_notification():
    message = notify.Message(
        severity="error",
        state=FIRING,
        title="cluster us reports no devices",
        rule=rules.CLUSTER_NO_CAPACITY,
        cluster="us",
        subject="clu_1",
        details={"nodes": 0},
    )
    payload = notify.slack_payload(message)
    assert "cluster us reports no devices" in payload["text"]
    assert payload["blocks"]
    assert "clu_1" in payload["blocks"][1]["elements"][0]["text"]


def test_a_resolution_reads_as_a_resolution_not_as_a_quieter_alarm():
    message = notify.Message(
        severity="error",
        state=RESOLVED,
        title="cluster us recovered",
        rule="r",
        cluster="us",
        subject=None,
        details={},
    )
    assert "Resolved" in notify.slack_payload(message)["text"]


def test_channels_below_the_floor_are_not_written_to():
    class Channel:
        def __init__(self, min_severity, enabled=True, kind="slack"):
            self.min_severity = min_severity
            self.enabled = enabled
            self.kind = kind
            self.name = min_severity

    channels = [Channel("critical"), Channel("warning"), Channel("info", enabled=False)]
    routed = notify.routed_to(channels, FakeAlert(FIRING, None, severity="error"))
    assert [c.min_severity for c in routed] == ["warning"]


# --------------------------------------------------------------------------- tuning


def test_durations_are_readable_in_the_units_people_write_them_in():
    assert tuning.parse_duration("45m") == 2700
    assert tuning.parse_duration("2h") == 7200
    assert tuning.parse_duration("900s") == 900
    assert tuning.parse_duration("900") == 900
    assert tuning.parse_duration("1d") == 86400


def test_a_typo_in_a_label_is_ignored_rather_than_obeyed_or_raised():
    """A bad label must not stop the tick that would have said the cluster is down."""
    assert tuning.parse_duration("soon") is None
    assert tuning.parse_duration("") is None
    assert tuning.read({"hangar.dev/alert.severity": "extremely"}).severity is None
    assert tuning.read({"hangar.dev/alert.for": "later"}).for_seconds is None


def test_an_object_with_no_opinion_leaves_the_intent_exactly_as_it_was():
    intent = rules.deployment_behind(None, "us", "dep_1", "llama", 0, 2)
    assert tuning.apply(intent, {}) is intent
    assert tuning.apply(intent, None) is intent
    assert tuning.apply(intent, {"team": "inference"}) is intent


def test_a_deployment_can_be_muted_from_its_labels():
    intent = rules.deployment_behind(None, "us", "dep_1", "llama", 0, 2)
    assert tuning.apply(intent, {"hangar.dev/alert": "off"}) is None
    assert tuning.apply(intent, {"hangar.dev/alert": "on"}) is intent


def test_severity_and_window_can_be_overridden_independently():
    intent = rules.deployment_behind(None, "us", "dep_1", "llama", 0, 2)
    quieter = tuning.apply(intent, {"hangar.dev/alert.severity": "warning"})
    assert quieter.severity == rules.WARNING
    assert quieter.for_seconds == intent.for_seconds

    patient = tuning.apply(intent, {"hangar.dev/alert.for": "2h"})
    assert patient.for_seconds == 7200
    assert patient.severity == intent.severity


def test_a_tuned_alert_says_what_tuned_it():
    """Otherwise "why did this fire as a warning" has no answer short of guessing."""
    intent = rules.workload_stalled(None, "us", "dsw_1", "serving/llama", 0, 1, promoted=False)
    tuned = tuning.apply(intent, {"hangar.dev/alert.severity": "info", "hangar.dev/alert.for": "30m"})
    assert tuned.details["tuned_by"] == {"severity": "info", "for": 1800}
    assert tuned.details["replicas_ready"] == 0


def test_tuning_never_changes_the_identity_of_an_alert():
    """A dedupe key that moved when someone edited a label would fork one problem into two."""
    intent = rules.deployment_behind(None, "us", "dep_1", "llama", 0, 2)
    tuned = tuning.apply(intent, {"hangar.dev/alert.severity": "info"})
    assert tuned.dedupe_key == intent.dedupe_key


def test_more_specific_labels_win_field_by_field():
    """A deployment setting only the window still inherits the severity from above it."""
    resolved = tuning.resolve({"hangar.dev/alert.for": "10m"}, {"hangar.dev/alert.severity": "critical"})
    assert resolved.for_seconds == 600
    assert resolved.severity == rules.CRITICAL


def test_a_mute_anywhere_in_the_chain_mutes():
    assert tuning.resolve({}, {"hangar.dev/alert": "off"}).muted
    assert tuning.resolve({"hangar.dev/alert": "off"}, {}).muted


# --------------------------------------------------------------------------- liveness


def _sql(statement) -> str:
    from sqlalchemy.dialects import postgresql

    return str(statement.compile(dialect=postgresql.dialect()))


def test_a_cluster_is_unheard_from_only_after_several_missed_polls(monkeypatch):
    from app.config import settings
    from app.reactor import liveness

    monkeypatch.setattr(settings, "HUB_OWNERSHIP_CHECK_SECONDS", 30.0)
    assert liveness.stale_after() == dt.timedelta(seconds=180)
    monkeypatch.setattr(settings, "HUB_OWNERSHIP_CHECK_SECONDS", 1.0)
    assert liveness.stale_after() == dt.timedelta(seconds=60), "a short poll interval does not make it twitchy"


def test_an_abandoned_claim_is_judged_on_the_database_clock():
    from app.reactor import liveness

    sql = _sql(liveness.expire_claims(dt.timedelta(seconds=60)))
    assert sql.startswith("DELETE FROM hub_cluster_claim")
    assert "heartbeat_at < now() -" in sql


def test_only_a_cluster_still_claimed_reachable_and_gone_quiet_is_marked_unreachable():
    from app.reactor import liveness

    moment = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
    statement = liveness.unseen_clusters(moment, dt.timedelta(seconds=60))
    sql = _sql(statement)
    assert sql.startswith("UPDATE supply_cluster_sync_state SET")
    assert "reachable=%(reachable)s" in sql
    assert "reachable IS true" in sql
    assert "last_seen_at IS NULL OR supply_cluster_sync_state.last_seen_at <" in sql
    params = statement.compile().params
    assert params["last_seen_at_1"] == moment - dt.timedelta(seconds=60)


# --------------------------------------------------------------------------- event cursor and delivery


def test_a_seq_the_cursor_stepped_over_is_watched_until_it_shows_up():
    from app.reactor.loop import LateCommits

    late = LateCommits(window=60)
    moment = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
    late.skipped(10, 15, {11, 12, 15}, moment)

    assert late.due(moment) == [13, 14]
    late.found({13})
    assert late.due(moment) == [14]


def test_a_watched_seq_that_never_commits_is_forgotten_after_the_window():
    from app.reactor.loop import LateCommits

    late = LateCommits(window=60)
    moment = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
    late.skipped(0, 3, {3}, moment)

    assert late.due(moment + dt.timedelta(seconds=61)) == []
    assert len(late) == 0


def test_a_wide_step_watches_only_the_seqs_just_below_the_ones_it_saw():
    from app.reactor.loop import MAX_GAP
    from app.reactor.loop import LateCommits

    late = LateCommits(window=60)
    moment = dt.datetime(2026, 10, 15, tzinfo=dt.UTC)
    late.skipped(0, 10**9, {10**9}, moment)
    assert late.due(moment) == list(range(10**9 - MAX_GAP, 10**9))


def test_a_reset_cursor_still_watches_the_holes_between_the_seqs_it_read():
    """From cursor 0 over a long log: the hole between two seen seqs is what a late commit leaves."""
    from app.reactor.loop import LateCommits

    late = LateCommits(window=60)
    moment = dt.datetime(2026, 10, 15, tzinfo=dt.UTC)
    late.skipped(0, 900003, {900001, 900003}, moment)

    due = late.due(moment)
    assert 900002 in due
    assert 900001 not in due
    assert min(due) > 0, "the hole below the first seen seq is bounded, not enumerated from zero"


def test_the_watch_list_stops_at_its_cap():
    from app.reactor.loop import LateCommits

    late = LateCommits(window=60, cap=5)
    late.skipped(0, 100, {100}, dt.datetime(2026, 10, 15, tzinfo=dt.UTC))
    assert len(late) == 5


def test_a_channel_that_took_the_message_is_not_sent_it_again():
    from app.reactor.loop import DeliveryLedger

    ledger = DeliveryLedger()
    ledger.record("alr_1", "firing", "chn_ok")
    assert ledger.told("alr_1", "firing", "chn_ok")
    assert not ledger.told("alr_1", "resolved", "chn_ok"), "a new state is a new message"


def test_a_failing_channel_backs_off_on_a_doubling_number_of_ticks():
    from app.reactor.loop import DeliveryLedger

    ledger = DeliveryLedger()
    skipped = []
    for _ in range(12):
        if ledger.waiting("alr_1", "firing", "chn_bad"):
            skipped.append(True)
            continue
        skipped.append(False)
        ledger.failed("alr_1", "firing", "chn_bad")

    attempts = [index for index, skip in enumerate(skipped) if not skip]
    assert attempts == [0, 1, 3, 7]


def test_settling_an_alert_forgets_its_channels():
    from app.reactor.loop import DeliveryLedger

    ledger = DeliveryLedger()
    ledger.record("alr_1", "firing", "chn_ok")
    ledger.failed("alr_1", "firing", "chn_bad")
    ledger.settle("alr_1", "firing")
    assert not ledger.told("alr_1", "firing", "chn_ok")
    assert not ledger.waiting("alr_1", "firing", "chn_bad")


def test_an_alert_state_that_stopped_being_due_is_forgotten_even_with_a_channel_failing():
    from app.reactor.loop import DeliveryLedger

    ledger = DeliveryLedger()
    ledger.record("alr_1", "firing", "chn_ok")
    ledger.failed("alr_1", "firing", "chn_bad")
    ledger.record("alr_2", "firing", "chn_ok")

    ledger.keep_only({("alr_1", "resolved"), ("alr_2", "firing")})

    assert not ledger.told("alr_1", "firing", "chn_ok")
    assert not ledger.waiting("alr_1", "firing", "chn_bad")
    assert ledger.told("alr_2", "firing", "chn_ok")
    assert len(ledger) == 1


# --------------------------------------------------------------------------- repair


def test_a_short_workload_is_one_holding_fewer_managed_replicas_than_it_asks_for():
    from app.reactor import reconcile

    sql = _sql(reconcile.short_workloads())
    assert "demand_workload.paused IS false" in sql
    assert "demand_workload.desired_scale > coalesce(" in sql
    assert "demand_deployment.authority = " in sql
    assert "demand_workload.id NOT IN (SELECT demand_deployment.workload_id" in sql, "adopted workloads are left alone"


def test_only_an_open_operation_past_a_deadline_it_has_is_timed_out():
    from app.reactor import reconcile

    statement = reconcile.expire_operations(dt.datetime(2026, 10, 15, tzinfo=dt.UTC))
    sql = _sql(statement)
    assert sql.startswith("UPDATE ops_operation SET")
    assert "ops_operation.phase IN (" in sql
    assert "ops_operation.deadline_at IS NOT NULL" in sql
    assert "RETURNING ops_operation.uid" in sql
    params = statement.compile().params
    assert params["phase"] == "timed_out"


def test_an_ended_operation_is_never_moved_by_a_later_report():
    from app.ingestion import statements

    from tests.test_ingestion import CLUSTER_ID
    from tests.test_ingestion import payload

    sql = _sql(statements.update_operation(CLUSTER_ID, payload("operation.observed")))
    assert "ops_operation.phase NOT IN (" in sql


def test_the_repair_pass_runs_on_its_own_slower_clock():
    from app.reactor.reconcile import Cadence

    cadence = Cadence(interval=3600)
    assert cadence.due() is True
    assert cadence.due() is False


class SwallowingSessions:
    """A session factory whose connect turns its cancellation into a driver error."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    def __call__(self) -> "SwallowingSessions":
        return self

    async def __aenter__(self) -> None:
        self.entered.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise OSError("connection attempt interrupted") from None

    async def __aexit__(self, *exc) -> bool:
        return False


async def test_a_cancellation_the_driver_swallows_still_stops_the_reactor(monkeypatch):
    from app.config import settings
    from app.reactor.loop import reactor_loop

    monkeypatch.setattr(settings, "REACTOR_INTERVAL_SECONDS", 0)
    sessions = SwallowingSessions()
    task = asyncio.create_task(reactor_loop("test", sessions=sessions))
    await asyncio.wait_for(sessions.entered.wait(), 1)
    task.cancel()
    # Not wait_for: its own cancel on timeout would be swallowed the same way.
    await asyncio.wait([task], timeout=1)
    assert task.cancelled()
