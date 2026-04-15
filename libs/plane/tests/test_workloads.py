import inspect
import datetime as dt

import pytest

from app.core.exceptions import Invalid
from app.modules.workloads.routes import WorkloadRoutes
from app.modules.workloads.schemas import WorkloadSchema
from app.modules.workloads.schemas import WorkloadCreateRequest
from app.modules.workloads.schemas import WorkloadUpdateRequest
from app.modules.workloads.services import KINDS
from app.modules.workloads.services import UNITS
from app.modules.workloads.services import PHASES
from app.modules.workloads.services import STRATEGIES
from app.modules.workloads.services import _revision
from app.modules.workloads.services import check_scale
from app.modules.workloads.services import derive_phase
from app.modules.policies.repository import SUBJECTS
from app.modules.workloads.repository import WorkloadRepository


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


class FakeDeployment:
    def __init__(self, phase: str, stamp: dt.datetime | None = None):
        self.admission_phase = phase
        self.created_at = stamp or EPOCH
        self.updated_at = None


class FakeWorkload:
    def __init__(self, stamp: dt.datetime | None = None):
        self.created_at = stamp or EPOCH
        self.updated_at = None


@pytest.fixture
def repo():
    return WorkloadRepository(db=None)


def sql(query) -> str:
    return str(query)


def test_create_request_defaults_to_one_replica_of_a_service_with_no_cluster():
    data = WorkloadCreateRequest(recipe="llama-70b", name="serve", project="acme")
    assert data.kind == "service"
    assert data.cluster is None
    assert (data.min_scale, data.desired_scale, data.max_scale) == (1, 1, 1)
    assert data.strategy == "pack"
    assert data.max_clusters is None


def test_update_request_leaves_absent_fields_unset():
    data = WorkloadUpdateRequest(labels={"team": "search"})
    assert data.to_dict(exclude_unset=True) == {"labels": {"team": "search"}}


def test_scale_is_an_operation_and_not_a_field_write():
    fields = WorkloadUpdateRequest.__struct_fields__
    assert "desired_scale" not in fields
    assert "min_scale" not in fields
    assert "max_scale" not in fields


def test_a_workload_cannot_be_moved_between_recipes_or_clusters_by_patch():
    fields = WorkloadUpdateRequest.__struct_fields__
    assert "recipe" not in fields
    assert "cluster" not in fields


def test_workload_has_no_cluster_of_its_own():
    assert "cluster_id" not in WorkloadSchema.__struct_fields__
    assert "admission_phase" not in WorkloadSchema.__struct_fields__


def test_scale_bounds_must_be_ordered_and_non_negative():
    check_scale(0, 2, 4, "service", 1)
    with pytest.raises(Invalid):
        check_scale(-1, 1, 1, "service", 1)
    with pytest.raises(Invalid):
        check_scale(2, 1, 4, "service", 1)
    with pytest.raises(Invalid):
        check_scale(1, 4, 2, "service", 1)


def test_a_gang_has_no_range_only_its_size():
    check_scale(8, 8, 8, "gang", 8)
    with pytest.raises(Invalid):
        check_scale(4, 8, 8, "gang", 8)
    with pytest.raises(Invalid):
        check_scale(8, 8, 8, "gang", 4)


def test_phase_is_the_least_advanced_deployment_unless_one_failed():
    assert derive_phase([]) == "pending"
    assert derive_phase([FakeDeployment("running"), FakeDeployment("running")]) == "running"
    assert derive_phase([FakeDeployment("running"), FakeDeployment("pending")]) == "pending"
    assert derive_phase([FakeDeployment("running"), FakeDeployment("failed")]) == "failed"
    assert derive_phase([FakeDeployment("failed"), FakeDeployment("pending")]) == "failed"


def test_revision_moves_when_only_a_share_moved():
    workload = FakeWorkload()
    quiet = _revision(workload, [FakeDeployment("running")])
    scaled = FakeDeployment("running")
    scaled.updated_at = EPOCH + dt.timedelta(seconds=1)
    assert _revision(workload, [scaled]) != quiet


def test_the_unit_of_scale_is_named_by_kind():
    assert set(UNITS) == KINDS
    assert UNITS["service"] == "replicas"
    assert UNITS["gang"] == "ranks"
    assert UNITS["task"] == "completions"


def test_strategies_are_pack_and_spread_and_default_is_pack():
    assert STRATEGIES == frozenset({"pack", "spread"})
    assert WorkloadCreateRequest(recipe="r", name="n", project="p").strategy == "pack"


def test_phases_are_the_admission_phases_of_deployments():
    assert PHASES == frozenset({"pending", "admitted", "placed", "running", "succeeded", "failed"})


def test_a_workload_is_a_policy_subject():
    assert "workload" in SUBJECTS


def test_cluster_filter_matches_workloads_with_a_deployment_there(repo):
    query = sql(repo.list_query(limit=10, cluster="iad-1"))
    assert "EXISTS" in query.upper()
    assert "demand_deployment" in query


def test_phase_filter_reads_the_deployments_admission_phase(repo):
    query = sql(repo.list_query(limit=10, phase="running"))
    assert "admission_phase" in query


def test_deployments_are_fetched_for_a_whole_page_at_once(repo):
    query = sql(repo.deployment_query(["a", "b"]))
    assert "IN " in query.upper()
    assert "supply_cluster" in query


def test_routes_are_the_five_a_user_needs():
    names = {n for n, _ in inspect.getmembers(WorkloadRoutes)}
    assert {
        "list_workloads",
        "create_workload",
        "get_workload",
        "update_workload",
        "delete_workload",
    } <= names
