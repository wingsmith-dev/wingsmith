import datetime as dt

import pytest

from app.core.ids import NODE
from app.core.ids import EVENT
from app.core.ids import PROJECT
from app.core.ids import PREFIXES
from app.core.ids import is_uid
from app.core.ids import new_uid
from app.core.exceptions import Invalid
from app.modules.events.routes import EventRoutes
from app.modules.events.schemas import EventSchema
from app.modules.events.services import EventService
from app.modules.events.repository import EventRepository
from app.modules.events.repository import subject_subquery


@pytest.fixture
def repo():
    return EventRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_list_is_newest_first(repo):
    text = sql(repo.list_query(limit=10))
    assert "ORDER BY ops_event.seq DESC" in text


def test_cursor_walks_seq_downwards_not_upwards(repo):
    text = sql(repo.list_query(limit=10, before_seq=900))
    assert "ops_event.seq < " in text
    assert "ops_event.seq > " not in text


def test_ordering_is_not_on_occurred_at(repo):
    """`occurred_at` is cluster-written, so it is neither unique nor monotonic."""
    assert "ORDER BY ops_event.occurred_at" not in sql(repo.list_query(limit=10))


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            cluster="iad-1",
            kind="node.joined",
            severity="warning",
            since=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            until=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
        )
    )
    assert "LEFT OUTER JOIN supply_cluster" in text
    assert "supply_cluster.name = " in text
    assert "ops_event.type = " in text
    assert "ops_event.severity = " in text
    assert "ops_event.occurred_at >= " in text
    assert "ops_event.occurred_at < " in text


def test_cluster_filter_accepts_an_id(repo):
    uid = "clu_" + new_uid(NODE)[4:]
    assert "supply_cluster.uid = " in sql(repo.list_query(limit=10, cluster=uid))


def test_subject_filter_goes_through_the_subjects_own_table(repo):
    text = sql(repo.list_query(limit=10, subject=new_uid(NODE)))
    assert "ops_event.subject_id IN (SELECT supply_node.id" in text


def test_subject_must_be_an_identifier():
    with pytest.raises(Invalid):
        subject_subquery("iad-1-node-004")


def test_subject_of_an_unknown_kind_is_rejected():
    with pytest.raises(Invalid):
        subject_subquery(new_uid(PROJECT))


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_service_has_no_write_methods():
    for name in ("create", "update", "patch", "delete", "add"):
        assert not hasattr(EventService, name)


def test_controller_is_read_only():
    handlers = {h.handler_name for h in EventRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {"list_events", "get_event"}


def test_event_carries_no_measurement_fields():
    fields = set(EventSchema.__struct_fields__)
    measurements = {"utilization", "temperature", "throughput", "latency_ms", "tokens", "memory_used_mib", "power_w"}
    assert not fields & measurements


def test_event_has_no_revision_because_nothing_may_edit_one():
    assert "revision" not in EventSchema.__struct_fields__


def test_event_separates_when_it_happened_from_when_we_recorded_it():
    fields = EventSchema.__struct_fields__
    assert "occurred_at" in fields
    assert "recorded_at" in fields


def test_kind_is_the_wire_name_for_the_type_column(repo):
    assert "ops_event.type = " in sql(repo.list_query(limit=5, kind="device.degraded"))


def test_get_resolves_on_the_public_id_not_the_row_key(repo):
    text = sql(repo.get_query(new_uid(EVENT)))
    assert "ops_event.uid = " in text
    assert "ops_event.id = " not in text


def test_the_event_prefix_is_registered():
    assert EVENT in PREFIXES
    assert is_uid(new_uid(EVENT))
