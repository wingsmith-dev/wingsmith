import uuid
import inspect
import datetime as dt

import pytest

from app.core.ids import DEVICE
from app.core.ids import ALLOCATION
from app.core.ids import new_uid
from app.modules.allocations.routes import AllocationRoutes
from app.modules.allocations.schemas import AllocationSchema
from app.modules.allocations.schemas import AllocationDeviceSchema
from app.modules.allocations.services import AllocationService
from app.modules.allocations.services import gang_complete
from app.modules.allocations.repository import AllocationRepository
from app.modules.allocations.repository import device_ids


class FakeAllocation:
    def __init__(self, device_count: int, released_at=None) -> None:
        self.device_count = device_count
        self.released_at = released_at


@pytest.fixture
def repo():
    return AllocationRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_the_service_has_no_write_methods_at_all():
    """Absent, not raising. Ingestion is the only writer of an observed object."""
    for name in ("create", "update", "delete", "add", "release", "patch"):
        assert not hasattr(AllocationService, name)


def test_the_service_reads_and_nothing_else():
    public = {n for n in vars(AllocationService) if not n.startswith("_")}
    assert public == {"get", "list", "schema", "member"}


def test_the_repository_has_no_write_methods_either():
    for name in ("add", "delete", "update"):
        assert not hasattr(AllocationRepository, name)


def test_controller_exposes_only_the_two_reads():
    handlers = {h.handler_name for h in AllocationRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {"list_allocations", "get_allocation"}


def test_controller_path_matches_the_contract():
    assert AllocationRoutes.path == "/allocations"


def test_which_allocation_holds_this_device_is_an_index_lookup_not_a_scan(repo):
    text = sql(repo.holder_query(new_uid(DEVICE)))
    assert text.startswith("SELECT bind_allocation_device.allocation_id")
    assert "bind_allocation_device.released_at IS NULL" in text
    assert "bind_allocation_device.device_id IN (SELECT supply_device.id" in text


def test_a_device_is_referenced_by_prefixed_id_or_vendor_uuid():
    assert "supply_device.uid = " in sql(device_ids(new_uid(DEVICE)))
    assert "supply_device.device_uuid = " in sql(device_ids("GPU-abc"))


def test_the_device_filter_reuses_the_live_holder_lookup(repo):
    text = sql(repo.list_query(limit=10, device=new_uid(DEVICE)))
    assert "bind_allocation.id IN (SELECT bind_allocation_device.allocation_id" in text
    assert "bind_allocation_device.released_at IS NULL" in text


def test_get_query_resolves_the_public_id(repo):
    assert "bind_allocation.uid = " in sql(repo.get_query(new_uid(ALLOCATION)))


def test_a_reference_that_is_not_an_allocation_id_matches_nothing_rather_than_erroring(repo):
    """A raw key, a name, and another object's id are all not-an-allocation."""
    assert "bind_allocation.uid IS NULL" in sql(repo.get_query("not-an-allocation"))
    assert "bind_allocation.uid IS NULL" in sql(repo.get_query(str(uuid.uuid4())))
    assert "bind_allocation.uid IS NULL" in sql(repo.get_query(new_uid(DEVICE)))


def test_base_query_joins_cluster_project_and_replica(repo):
    text = sql(repo.list_query(limit=10))
    assert "JOIN supply_cluster ON bind_allocation.cluster_id = supply_cluster.id" in text
    assert "JOIN bind_project ON bind_allocation.project_id = bind_project.id" in text
    assert "LEFT OUTER JOIN demand_replica" in text
    assert "ORDER BY bind_allocation.seq ASC" in text


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            after_seq=9,
            cluster="iad-1",
            project="research",
            device_class="h100",
            phase="held",
            over_quota=True,
        )
    )
    assert "bind_allocation.seq > " in text
    assert "bind_allocation.cluster_id IN (SELECT supply_cluster.id" in text
    assert "bind_allocation.project_id IN (SELECT bind_project.id" in text
    assert "bind_allocation.device_class = " in text
    assert "bind_allocation.phase = " in text
    assert "bind_allocation.over_quota IS true" in text


def test_live_filter_is_expressed_as_the_release_timestamp(repo):
    assert "bind_allocation.released_at IS NULL" in sql(repo.list_query(limit=10, live=True))
    assert "bind_allocation.released_at IS NOT NULL" in sql(repo.list_query(limit=10, live=False))


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_members_are_fetched_for_a_whole_page_in_one_query(repo):
    text = sql(repo.member_query([uuid.uuid4(), uuid.uuid4()]))
    assert "JOIN supply_device ON bind_allocation_device.device_id = supply_device.id" in text
    assert "JOIN supply_node ON supply_device.node_id = supply_node.id" in text
    assert "bind_allocation_device.allocation_id IN " in text


def test_the_devices_an_allocation_holds_are_on_the_response():
    assert "devices" in AllocationSchema.__struct_fields__
    assert set(AllocationDeviceSchema.__struct_fields__) == {
        "device",
        "device_uuid",
        "node",
        "live",
        "released_at",
    }


def test_no_telemetry_is_carried_on_an_allocation():
    fields = set(AllocationSchema.__struct_fields__)
    assert not fields & {"utilization", "memory_used_mib", "temperature_c", "throughput"}


@pytest.mark.parametrize(
    ("count", "live_devices", "expected"),
    [(8, 8, True), (8, 7, False), (8, 0, False), (1, 1, True), (0, 0, True)],
)
def test_a_gang_is_complete_only_while_it_holds_every_device(count, live_devices, expected):
    assert gang_complete(FakeAllocation(count), live_devices) is expected


def test_a_released_allocation_is_not_an_incomplete_gang():
    released = FakeAllocation(8, released_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    assert gang_complete(released, 0) is False


def test_class_filter_is_exposed_as_class_not_device_class():
    parameter = inspect.signature(AllocationRoutes.list_allocations.fn).parameters["device_class"]
    assert parameter.default.query == "class"


def test_no_if_match_because_there_is_nothing_here_to_conditionally_write():
    assert "if_match" not in inspect.signature(AllocationRoutes.list_allocations.fn).parameters
    assert "if_match" not in inspect.signature(AllocationRoutes.get_allocation.fn).parameters
