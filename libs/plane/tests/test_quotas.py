import uuid
import inspect

import pytest

from app.core.ids import QUOTA
from app.core.ids import new_uid
from app.core.exceptions import Invalid
from app.modules.quotas.routes import QuotaRoutes
from app.modules.quotas.schemas import QuotaSchema
from app.modules.quotas.schemas import QuotaUsageSchema
from app.modules.quotas.schemas import QuotaCreateRequest
from app.modules.quotas.schemas import QuotaUpdateRequest
from app.modules.quotas.services import EMPTY
from app.modules.quotas.services import headroom
from app.modules.quotas.services import usage_for
from app.modules.quotas.services import over_limit
from app.modules.quotas.services import validate_numbers
from app.modules.quotas.repository import QuotaRepository


class FakeQuota:
    def __init__(self, project_id, cluster_id, device_class="h100") -> None:
        self.project_id = project_id
        self.cluster_id = cluster_id
        self.device_class = device_class


@pytest.fixture
def repo():
    return QuotaRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_quota_ids_are_lowercase_and_prefixed():
    uid = new_uid(QUOTA)
    assert uid.startswith("qta_")
    assert uid == uid.lower()


def test_declared_limits_and_observed_usage_sit_side_by_side_never_merged():
    fields = set(QuotaSchema.__struct_fields__)
    assert {"guaranteed_devices", "limit_devices", "over_quota_weight"} <= fields
    assert "usage" in fields
    assert "allocated_devices" not in fields
    assert set(QuotaUsageSchema.__struct_fields__) == {
        "allocated_devices",
        "live_allocations",
        "over_quota_devices",
    }


def test_the_scope_of_a_quota_cannot_be_changed_by_patch():
    fields = QuotaUpdateRequest.__struct_fields__
    assert "project" not in fields
    assert "cluster" not in fields
    assert "device_class" not in fields


def test_update_request_leaves_absent_fields_unset():
    assert QuotaUpdateRequest(guaranteed_devices=4).to_dict(exclude_unset=True) == {"guaranteed_devices": 4}


def test_update_request_can_clear_the_limit_with_null():
    assert QuotaUpdateRequest(limit_devices=None).to_dict(exclude_unset=True) == {"limit_devices": None}


def test_create_request_defaults_to_a_clusterless_over_quota_enabled_row():
    data = QuotaCreateRequest(project="research", device_class="h100")
    assert data.cluster is None
    assert data.guaranteed_devices == 0
    assert data.limit_devices is None
    assert data.over_quota_enabled is True


def test_get_query_resolves_by_id_only(repo):
    assert "bind_quota.uid = " in sql(repo.get_query(new_uid(QUOTA)))


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            after_seq=5,
            project="research",
            cluster="iad-1",
            device_class="h100",
            over_quota_enabled=True,
        )
    )
    assert "JOIN bind_project ON bind_quota.project_id = bind_project.id" in text
    assert "LEFT OUTER JOIN supply_cluster" in text
    assert "bind_quota.seq > " in text
    assert "bind_quota.project_id IN (SELECT bind_project.id" in text
    assert "bind_quota.cluster_id IN (SELECT supply_cluster.id" in text
    assert "bind_quota.device_class = " in text
    assert "bind_quota.over_quota_enabled IS true" in text
    assert "ORDER BY bind_quota.seq ASC" in text


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=50).compile().params["param_1"] == 51


def test_usage_is_an_aggregate_over_live_allocations_not_a_stored_counter(repo):
    text = sql(repo.usage_query([uuid.uuid4()]))
    assert "count(bind_allocation_device.id)" in text
    assert "count(DISTINCT bind_allocation.id)" in text
    assert "GROUP BY bind_allocation.project_id, bind_allocation.cluster_id, bind_allocation.device_class" in text


def test_usage_query_counts_only_devices_and_allocations_that_are_still_held(repo):
    text = sql(repo.usage_query([uuid.uuid4()]))
    assert "bind_allocation_device.released_at IS NULL" in text
    assert "bind_allocation.released_at IS NULL" in text


def test_usage_for_a_cluster_scoped_quota_ignores_other_clusters():
    project, cluster, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rollup = {
        (project, cluster, "h100"): {"devices": 4, "allocations": 2, "over_quota_devices": 1},
        (project, other, "h100"): {"devices": 8, "allocations": 1, "over_quota_devices": 0},
    }
    usage = usage_for(FakeQuota(project, cluster), rollup)
    assert usage.allocated_devices == 4
    assert usage.live_allocations == 2
    assert usage.over_quota_devices == 1


def test_usage_for_a_clusterless_quota_sums_every_cluster():
    project, cluster, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rollup = {
        (project, cluster, "h100"): {"devices": 4, "allocations": 2, "over_quota_devices": 1},
        (project, other, "h100"): {"devices": 8, "allocations": 1, "over_quota_devices": 0},
    }
    usage = usage_for(FakeQuota(project, None), rollup)
    assert usage.allocated_devices == 12
    assert usage.live_allocations == 3


def test_usage_ignores_other_projects_and_other_device_classes():
    project, cluster = uuid.uuid4(), uuid.uuid4()
    rollup = {
        (uuid.uuid4(), cluster, "h100"): {"devices": 4, "allocations": 1, "over_quota_devices": 0},
        (project, cluster, "a100"): {"devices": 2, "allocations": 1, "over_quota_devices": 0},
    }
    assert usage_for(FakeQuota(project, None), rollup) == EMPTY


def test_a_quota_with_no_allocations_reports_zero_not_null():
    assert usage_for(FakeQuota(uuid.uuid4(), None), {}) == EMPTY


@pytest.mark.parametrize(
    ("limit", "allocated", "expected"),
    [(8, 4, 4), (8, 8, 0), (8, 10, -2), (None, 10, None)],
)
def test_headroom_is_null_only_when_there_is_no_limit(limit, allocated, expected):
    assert headroom(limit, allocated) == expected


@pytest.mark.parametrize(
    ("limit", "allocated", "expected"),
    [(8, 4, False), (8, 8, False), (8, 9, True), (None, 1000, False)],
)
def test_over_limit_needs_a_limit_to_be_over(limit, allocated, expected):
    assert over_limit(limit, allocated) is expected


def test_a_limit_below_the_guarantee_is_refused():
    with pytest.raises(Invalid):
        validate_numbers(guaranteed=8, limit=4, weight=1.0)


def test_negative_numbers_are_refused():
    for args in ((-1, None, 1.0), (0, -1, 1.0), (0, None, -1.0)):
        with pytest.raises(Invalid):
            validate_numbers(*args)


def test_an_unlimited_quota_with_a_guarantee_is_valid():
    validate_numbers(guaranteed=8, limit=None, weight=2.5)


def test_controller_exposes_full_crud_on_a_declared_object():
    handlers = {h.handler_name for h in QuotaRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {"list_quotas", "create_quota", "get_quota", "update_quota", "delete_quota"}


def test_class_filter_is_exposed_as_class_not_device_class():
    parameter = inspect.signature(QuotaRoutes.list_quotas.fn).parameters["device_class"]
    assert parameter.default.query == "class"


def test_update_accepts_if_match_against_the_revision():
    parameter = inspect.signature(QuotaRoutes.update_quota.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"
