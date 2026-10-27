"""The alerts and notification-channels modules, compiled but not executed.

Query shape and schema surface only; the lifecycle lives in `test_reactor_db.py`,
where it needs a database to mean anything.
"""

import inspect

import pytest

from app.core.ids import ALERT
from app.core.ids import PREFIXES
from app.core.ids import NOTIFICATION_CHANNEL
from app.core.ids import is_uid
from app.core.ids import new_uid
from app.modules.alerts.routes import AlertRoutes
from app.modules.alerts.schemas import AlertSchema
from app.modules.alerts.schemas import AlertAcknowledgeRequest
from app.modules.alerts.services import AlertService
from app.modules.alerts.repository import AlertRepository
from app.modules.notifications.schemas import NotificationChannelSchema
from app.modules.notifications.services import NotificationChannelService
from app.modules.notifications.services import _hint
from app.modules.notifications.repository import NotificationChannelRepository


@pytest.fixture
def repo():
    return AlertRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_alert_prefixes_are_registered():
    assert is_uid(new_uid(ALERT))
    assert is_uid(new_uid(NOTIFICATION_CHANNEL))
    assert {ALERT, NOTIFICATION_CHANNEL} <= PREFIXES


def test_list_is_newest_first_on_seq(repo):
    assert "ORDER BY ops_alert.seq DESC" in sql(repo.list_query(limit=10))


def test_filters_compile_into_one_statement(repo):
    text = sql(repo.list_query(limit=10, cluster="us", state="firing", severity="error", rule="node.left"))
    assert "LEFT OUTER JOIN supply_cluster" in text
    assert "ops_alert.state = " in text
    assert "ops_alert.severity = " in text
    assert "ops_alert.rule = " in text


def test_a_subject_filter_matches_the_public_id_directly(repo):
    """No subquery, unlike events: an alert stores the uid, so a node that is gone still matches."""
    text = sql(repo.list_query(limit=10, subject="nod_01j9x2bbbbbbbbbbbbbbbbbbbb"))
    assert "ops_alert.subject_uid = " in text
    assert "SELECT supply_node.id" not in text


def test_the_service_cannot_create_or_delete_an_alert():
    """Derived state: the reactor opens alerts, and nothing else may invent or erase one."""
    assert not hasattr(AlertService, "create")
    assert not hasattr(AlertService, "delete")
    assert hasattr(AlertService, "acknowledge")
    assert hasattr(AlertService, "resolve")


def test_alerts_expose_no_create_route():
    handlers = {name for name in dir(AlertRoutes) if name.endswith(("_alert", "_alerts"))}
    assert "create_alert" not in handlers
    assert {"acknowledge_alert", "resolve_alert"} <= handlers


def test_the_state_filter_keeps_its_wire_name():
    """Litestar reserves `state`, and the contract still calls the filter `state`."""
    signature = inspect.signature(AlertRoutes.list_alerts.fn)
    assert "state" not in signature.parameters
    assert signature.parameters["alert_state"].default.query == "state"


def test_the_acknowledger_is_not_taken_from_the_request_body():
    """`acknowledged_by` is read back but never written by the caller."""
    assert "acknowledged_by" in AlertSchema.__struct_fields__
    assert AlertAcknowledgeRequest.__struct_fields__ == ("note",)


def test_a_webhook_is_never_returned_in_full():
    """A Slack incoming webhook is a bearer credential; a hint is all a list may show."""
    assert "webhook_url" not in NotificationChannelSchema.__struct_fields__
    assert "webhook_hint" in NotificationChannelSchema.__struct_fields__


def test_the_hint_identifies_a_webhook_without_reproducing_it():
    url = "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXsecret"
    hint = _hint(url)
    assert hint == "hooks.slack.com/…cret"
    assert "T00000000" not in hint
    assert _hint(None) is None


def test_channels_are_addressable_by_name_as_well_as_id():
    repo = NotificationChannelRepository(db=None)
    assert "ops_notification_channel.name = " in str(repo._ref_clause("ops"))
    assert "ops_notification_channel.uid = " in str(repo._ref_clause(new_uid(NOTIFICATION_CHANNEL)))


def test_the_channel_service_is_ordinary_crud():
    """Declared configuration, so unlike alerts it gets the full set."""
    for name in ("create", "update", "delete", "test"):
        assert hasattr(NotificationChannelService, name)
