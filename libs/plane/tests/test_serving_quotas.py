"""The second kind of quota: traffic, enforced in the gateway, measured nowhere here.

Every test in this file is offline. That is not a limitation of the suite, it is
the property under test — a serving quota is declared state and a resolution over
rows this database already owns, so nothing here needs a counter, a metrics
backend or a request to have happened.
"""

import inspect
import datetime as dt

import pytest

from app.core.ids import ENDPOINT
from app.core.ids import SERVING_QUOTA
from app.core.ids import new_uid
from app.core.exceptions import Invalid
from app.core.exceptions import Conflict
from app.modules.quotas.routes import QuotaRoutes
from app.modules.quotas.routes import ServingQuotaRoutes
from app.modules.quotas.schemas import QuotaSchema
from app.modules.quotas.schemas import ServingQuotaSchema
from app.modules.quotas.schemas import ResolvedLimitsSchema
from app.modules.quotas.schemas import ServingQuotaCreateRequest
from app.modules.quotas.schemas import ServingQuotaUpdateRequest
from app.modules.quotas.schemas import ResolvedServingQuotaSchema
from app.modules.quotas.services import ON_EXCEED
from app.modules.quotas.services import LIMIT_SCOPES
from app.modules.quotas.services import STANDARD_RPM
from app.modules.quotas.services import STANDARD_TPM
from app.modules.quotas.services import DIVISIBLE_LIMITS
from app.modules.quotas.services import STANDARD_SUCCESS_RATE
from app.modules.quotas.services import ServingQuotaService
from app.modules.quotas.services import _stamp
from app.modules.quotas.services import check_fleet
from app.modules.quotas.services import standard_of
from app.modules.quotas.services import replica_share
from app.modules.quotas.services import fleet_revision
from app.modules.quotas.services import declared_limits
from app.modules.quotas.services import effective_limits
from app.modules.quotas.services import starved_replicas
from app.modules.quotas.services import validate_serving
from app.modules.quotas.services import output_length_params
from app.modules.endpoints.schemas import ResolvedEndpointSchema
from app.modules.quotas.repository import ServingQuotaRepository
from app.modules.endpoints.services import _served
from app.modules.endpoints.services import _revision_of
from app.modules.endpoints.repository import EndpointRepository


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


class FakeQuota:
    """A serving quota row, without a database under it."""

    def __init__(self, **kwargs) -> None:
        defaults = {
            "id": id(self),
            "committed_rpm": None,
            "committed_tpm": None,
            "max_concurrent_requests": None,
            "max_output_tokens": None,
            "burst_rpm": None,
            "ramp_up_seconds": 0,
            "on_exceed": "reject",
            "queue_timeout_ms": None,
            "limit_scope": "per_replica",
            "success_rate_objective": None,
            "availability_objective": "availability-2026.1",
            "objective_window_seconds": 600,
            "enforced": False,
        }
        defaults.update(kwargs)
        for key, value in defaults.items():
            setattr(self, key, value)


def divided(**kwargs) -> FakeQuota:
    return FakeQuota(limit_scope="divided", **kwargs)


@pytest.fixture
def repo():
    return ServingQuotaRepository(db=None)


def sql(query) -> str:
    return str(query)


def full(**overrides) -> dict:
    fields = {
        "committed_rpm": 1_000,
        "committed_tpm": 10_000_000,
        "max_concurrent_requests": 64,
        "max_output_tokens": None,
        "burst_rpm": 2_000,
        "ramp_up_seconds": 0,
        "on_exceed": "reject",
        "queue_timeout_ms": None,
        "limit_scope": "per_replica",
        "success_rate_objective": 0.999,
        "availability_objective": "availability-2026.1",
        "objective_window_seconds": 600,
    }
    fields.update(overrides)
    return fields


# --- identity and the two objects that share the module -----------------------


def test_serving_quota_ids_are_lowercase_and_prefixed():
    uid = new_uid(SERVING_QUOTA)
    assert uid.startswith("sqt_")
    assert uid == uid.lower()


def test_the_two_quotas_in_this_module_share_no_limit():
    """They share the tenancy pointer and nothing that means "how much"."""
    common = {"id", "created_at", "updated_at", "revision", "project"}
    device = set(QuotaSchema.__struct_fields__) - common
    serving = set(ServingQuotaSchema.__struct_fields__) - common
    assert not device & serving
    assert "over_quota_weight" not in serving
    assert "devices" not in " ".join(serving)


def test_the_declared_surface_carries_no_measurement():
    """Rule 4: what these numbers cost is counted in the gateway and stays there."""
    measured = ("usage", "observed", "consumed", "throttled", "breach", "measured", "actual")
    for struct in (ServingQuotaSchema, ResolvedLimitsSchema, ResolvedServingQuotaSchema):
        for field in struct.__struct_fields__:
            assert not [word for word in measured if word in field], (struct, field)
            assert not field.startswith(("current_", "used_", "last_")), (struct, field)


def test_the_scope_of_a_serving_quota_cannot_be_changed_by_patch():
    assert "endpoint" not in ServingQuotaUpdateRequest.__struct_fields__


def test_create_defaults_are_an_unlimited_unenforced_per_replica_row():
    data = ServingQuotaCreateRequest(endpoint="chat")
    assert data.committed_rpm is None
    assert data.committed_tpm is None
    assert data.enforced is False
    assert data.limit_scope == "per_replica"
    assert data.on_exceed == "reject"


def test_update_request_leaves_absent_fields_unset():
    assert ServingQuotaUpdateRequest(enforced=True).to_dict(exclude_unset=True) == {"enforced": True}


def test_update_request_can_withdraw_one_limit_with_null():
    assert ServingQuotaUpdateRequest(committed_rpm=None).to_dict(exclude_unset=True) == {"committed_rpm": None}


# --- the enforcement mode -----------------------------------------------------


def test_both_enforcement_modes_are_representable():
    assert LIMIT_SCOPES == {"per_replica", "divided"}


def test_per_replica_hands_every_replica_the_whole_number():
    quota = FakeQuota(committed_rpm=1_000, committed_tpm=10_000_000, max_concurrent_requests=64)
    for index in range(4):
        assert effective_limits(quota, replicas=4, index=index) == declared_limits(quota)


def test_divided_at_one_replica_is_the_whole_number_too():
    quota = divided(committed_rpm=1_000)
    assert effective_limits(quota, replicas=1, index=0).committed_rpm == 1_000


def test_divided_splits_evenly_when_it_divides():
    quota = divided(committed_rpm=1_000, committed_tpm=10_000_000, max_concurrent_requests=64)
    for index in range(4):
        limits = effective_limits(quota, replicas=4, index=index)
        assert limits.committed_rpm == 250
        assert limits.committed_tpm == 2_500_000
        assert limits.max_concurrent_requests == 16


@pytest.mark.parametrize(("replicas", "expected"), [(3, [334, 333, 333]), (7, [143, 143, 143, 143, 143, 143, 142])])
def test_an_uneven_split_gives_the_remainder_to_the_lowest_replicas(replicas, expected):
    quota = divided(committed_rpm=1_000)
    assert [effective_limits(quota, replicas, i).committed_rpm for i in range(replicas)] == expected


@pytest.mark.parametrize("replicas", [1, 2, 3, 5, 7, 11, 13, 64, 999, 1_001])
def test_the_shares_sum_to_exactly_what_was_sold_at_every_replica_count(replicas):
    """The whole reason the split is largest-remainder and not a floor."""
    for limit in (1_000, 10_000_000, 3, 1):
        shares = [replica_share(limit, replicas, i) for i in range(replicas)]
        assert sum(shares) == limit


@pytest.mark.parametrize("replicas", [1, 2, 3, 5, 8])
def test_no_replica_is_handed_more_than_the_ceiling_or_less_than_the_floor(replicas):
    limit = 1_000
    shares = [replica_share(limit, replicas, i) for i in range(replicas)]
    assert max(shares) - min(shares) <= 1
    assert min(shares) == limit // replicas


def test_a_share_is_stable_for_one_index():
    assert replica_share(1_000, 3, 0) == replica_share(1_000, 3, 0) == 334


def test_an_uncommitted_limit_stays_uncommitted_rather_than_becoming_zero():
    quota = divided(committed_rpm=1_000)
    limits = effective_limits(quota, replicas=8, index=7)
    assert limits.committed_tpm is None
    assert limits.max_concurrent_requests is None
    assert replica_share(None, 8, 7) is None


def test_burst_never_falls_below_the_sustained_rate_after_dividing():
    """The invariant the validator enforces has to survive the arithmetic."""
    quota = divided(committed_rpm=1_000, burst_rpm=1_001)
    for replicas in (2, 3, 7, 13, 999):
        for index in range(min(replicas, 20)):
            limits = effective_limits(quota, replicas, index)
            assert limits.burst_rpm >= limits.committed_rpm


def test_a_per_request_ceiling_is_never_divided():
    quota = divided(committed_rpm=1_000, max_output_tokens=4_096)
    assert effective_limits(quota, replicas=4, index=3).max_output_tokens == 4_096
    assert "max_output_tokens" not in DIVISIBLE_LIMITS


def test_a_duration_is_never_divided():
    quota = divided(committed_rpm=1_000, ramp_up_seconds=120, on_exceed="queue", queue_timeout_ms=500)
    limits = effective_limits(quota, replicas=3, index=2)
    assert limits.ramp_up_seconds == 120
    assert limits.queue_timeout_ms == 500


def test_the_mode_travels_with_the_numbers_so_the_reader_knows_how_to_read_them():
    assert "limit_scope" in ResolvedLimitsSchema.__struct_fields__
    assert declared_limits(divided(committed_rpm=1)).limit_scope == "divided"


def test_a_commitment_smaller_than_the_fleet_starves_the_highest_replicas():
    quota = divided(committed_rpm=2)
    assert [replica_share(2, 5, i) for i in range(5)] == [1, 1, 0, 0, 0]
    assert starved_replicas(quota, 5) == 3


def test_starvation_is_counted_across_every_divisible_limit():
    quota = divided(committed_rpm=1_000, max_concurrent_requests=2)
    assert starved_replicas(quota, 5) == 3


def test_per_replica_never_starves_anything():
    assert starved_replicas(FakeQuota(committed_rpm=2), 5) == 0


def test_a_fleet_that_fits_the_commitment_starves_nobody():
    assert starved_replicas(divided(committed_rpm=1_000), 4) == 0


def test_a_fleet_a_share_cannot_be_taken_of_is_refused():
    for replicas, index in ((0, 0), (-1, 0), (3, 3), (3, -1)):
        with pytest.raises(Invalid):
            check_fleet(replicas, index)


def test_a_well_formed_fleet_is_accepted():
    check_fleet(1, 0)
    check_fleet(999, 998)


# --- validation ---------------------------------------------------------------


def test_a_declared_limit_of_zero_is_a_typo_not_a_ban():
    for name in ("committed_rpm", "committed_tpm", "max_concurrent_requests", "burst_rpm", "max_output_tokens"):
        with pytest.raises(Invalid):
            validate_serving(full(**{name: 0}))


def test_a_burst_below_the_sustained_rate_is_refused():
    with pytest.raises(Invalid):
        validate_serving(full(committed_rpm=1_000, burst_rpm=500))


def test_a_burst_equal_to_the_sustained_rate_is_fine():
    validate_serving(full(committed_rpm=1_000, burst_rpm=1_000))


def test_a_success_rate_written_as_a_percentage_is_refused():
    with pytest.raises(Invalid):
        validate_serving(full(success_rate_objective=99.9))


def test_a_success_rate_of_one_is_the_only_boundary_that_passes():
    validate_serving(full(success_rate_objective=1.0))
    with pytest.raises(Invalid):
        validate_serving(full(success_rate_objective=0))


def test_a_queue_timeout_means_nothing_without_a_queue():
    with pytest.raises(Invalid):
        validate_serving(full(on_exceed="reject", queue_timeout_ms=200))


def test_a_queue_with_no_timeout_is_unbounded_and_refused():
    with pytest.raises(Invalid):
        validate_serving(full(on_exceed="queue", queue_timeout_ms=None))


def test_a_bounded_queue_is_accepted():
    validate_serving(full(on_exceed="queue", queue_timeout_ms=200))


def test_only_the_two_overflow_behaviours_exist():
    assert ON_EXCEED == {"reject", "queue"}
    with pytest.raises(Invalid):
        validate_serving(full(on_exceed="drop"))


def test_an_unknown_enforcement_mode_is_refused():
    with pytest.raises(Invalid):
        validate_serving(full(limit_scope="sharded"))


def test_an_unknown_availability_objective_is_refused_rather_than_defaulted():
    with pytest.raises(Invalid):
        validate_serving(full(availability_objective="availability-2029.9"))


@pytest.mark.parametrize("window", [59, 3_601, 0])
def test_an_objective_window_outside_the_usable_range_is_refused(window):
    with pytest.raises(Invalid):
        validate_serving(full(objective_window_seconds=window))


@pytest.mark.parametrize("window", [60, 600, 3_600])
def test_a_usable_objective_window_is_accepted(window):
    validate_serving(full(objective_window_seconds=window))


def test_a_negative_ramp_is_refused_and_zero_means_no_ramp():
    validate_serving(full(ramp_up_seconds=0))
    with pytest.raises(Invalid):
        validate_serving(full(ramp_up_seconds=-1))


def test_an_output_ceiling_needs_the_surface_to_say_where_it_is_read_from():
    validate_serving(full(max_output_tokens=4_096), surface={})
    validate_serving(full(max_output_tokens=4_096), surface={"output_length_params": ["max_tokens"]})
    with pytest.raises(Invalid):
        validate_serving(full(max_output_tokens=4_096), surface={"output_length_params": []})


def test_an_endpoint_that_has_said_nothing_speaks_the_ordinary_surface():
    assert output_length_params(None) == ["max_completion_tokens", "max_tokens", "max_output_tokens"]
    assert output_length_params({"output_length_params": ["n_predict"]}) == ["n_predict"]


# --- the acceptance standard --------------------------------------------------


def test_the_standard_is_the_three_floors_the_acceptance_criteria_name():
    assert (STANDARD_RPM, STANDARD_TPM, STANDARD_SUCCESS_RATE) == (1_000, 10_000_000, 0.999)


def test_a_commitment_exactly_on_the_floors_meets_the_standard():
    standard = standard_of(1_000, 10_000_000, 0.999)
    assert standard.meets is True
    assert standard.shortfalls == []


def test_each_floor_is_named_when_it_is_missed():
    assert standard_of(999, 10_000_000, 0.999).shortfalls == ["committed_rpm"]
    assert standard_of(1_000, 9_999_999, 0.999).shortfalls == ["committed_tpm"]
    assert standard_of(1_000, 10_000_000, 0.99).shortfalls == ["success_rate_objective"]


def test_committing_nothing_does_not_clear_a_floor():
    standard = standard_of(None, None, None)
    assert standard.meets is False
    assert standard.shortfalls == ["committed_rpm", "committed_tpm", "success_rate_objective"]


def test_the_standard_is_declared_against_declared_and_reads_nothing_measured():
    assert inspect.signature(standard_of).parameters.keys() == {
        "committed_rpm",
        "committed_tpm",
        "success_rate_objective",
    }


# --- the resolution path ------------------------------------------------------


def test_get_resolves_by_the_quota_id_or_by_the_endpoint_it_commits(repo):
    assert "bind_serving_quota.uid = " in sql(repo.get_query(new_uid(SERVING_QUOTA)))
    by_endpoint = sql(repo.get_query("chat"))
    assert "bind_serving_quota.endpoint_id IN (SELECT demand_endpoint.id" in by_endpoint
    assert "demand_endpoint.name = " in by_endpoint
    assert "demand_endpoint.uid = " in sql(repo.get_query(new_uid(ENDPOINT)))


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(limit=10, after_seq=5, endpoint="chat", project="acme", enforced=True, limit_scope="divided")
    )
    assert "JOIN demand_endpoint ON bind_serving_quota.endpoint_id = demand_endpoint.id" in text
    assert "JOIN bind_project ON demand_endpoint.project_id = bind_project.id" in text
    assert "bind_serving_quota.seq > " in text
    assert "demand_endpoint.project_id IN (SELECT bind_project.id" in text
    assert "bind_serving_quota.enforced IS true" in text
    assert "bind_serving_quota.limit_scope = " in text
    assert "ORDER BY bind_serving_quota.seq ASC" in text


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=50).compile().params["param_1"] == 51


def test_nothing_in_this_repository_counts_anything(repo):
    """The contrast with `QuotaRepository` is the point, so it is asserted."""
    assert not [name for name in dir(repo) if "usage" in name or "count" in name]


def test_resolving_by_workload_walks_targets_to_deployments_in_one_statement(repo):
    text = sql(repo.workload_endpoints_query("summariser"))
    assert "SELECT demand_endpoint_target.endpoint_id" in text
    assert "JOIN demand_deployment ON demand_endpoint_target.deployment_id = demand_deployment.id" in text
    assert "demand_deployment.workload_id IN (SELECT demand_workload.id" in text


def test_the_resolved_query_keeps_a_commitment_with_nothing_behind_it(repo):
    text = sql(repo.resolved_query())
    assert "LEFT OUTER JOIN demand_endpoint_target" in text
    assert "LEFT OUTER JOIN demand_deployment" in text
    assert "LEFT OUTER JOIN demand_workload" in text
    assert "demand_endpoint.enabled IS true" in text


def test_the_resolved_query_narrows_by_endpoint_or_by_workload(repo):
    by_endpoint = sql(repo.resolved_query(endpoint="chat"))
    assert "demand_endpoint.id IN (SELECT demand_endpoint.id" in by_endpoint
    by_workload = sql(repo.resolved_query(workload="summariser"))
    assert "demand_endpoint.id IN (SELECT demand_endpoint_target.endpoint_id" in by_workload


def test_a_disabled_target_contributes_no_workload_to_the_resolution(repo):
    assert "demand_endpoint_target.enabled IS true" in sql(repo.resolved_query())


def test_the_resolved_table_is_versioned_by_the_fleet_as_well_as_the_rows():
    assert fleet_revision([EPOCH], 3, 1) != fleet_revision([EPOCH], 3, 2)
    assert fleet_revision([EPOCH], 3, 1) != fleet_revision([EPOCH], 4, 1)
    assert fleet_revision([EPOCH], 3, 1) == fleet_revision([EPOCH], 3, 1)


def test_an_empty_resolution_still_has_a_revision_a_caller_can_send_back():
    assert fleet_revision([], 1, 0) == "0-0-1-0"


def test_a_withdrawn_commitment_moves_the_revision_even_when_it_was_not_the_newest():
    """A max alone cannot see a delete, which is how a `304` outlives a quota."""
    later = EPOCH + dt.timedelta(days=1)
    before = fleet_revision([EPOCH, later], 1, 0)
    after = fleet_revision([later], 1, 0)
    assert before != after


def test_the_routing_tables_revision_learned_the_same_lesson():
    later = EPOCH + dt.timedelta(days=1)
    served = ["acme/llama-70b"]
    assert _revision_of([EPOCH, later], served) != _revision_of([later], served)
    assert _revision_of([], served) == f"0-0-{_served(served)}"


def test_the_revision_moves_when_what_the_engines_answer_to_moves():
    """The third term is content, and it is the only one that can see this change.

    Renaming nothing and restamping nothing, a resolution whose engines now answer
    to a different model id is a different routing table: the gateway rewrites an
    inbound `model` to that string, so a gateway holding the old one behind a `304`
    rewrites to a name the engine was never told to answer to.
    """
    stamps = [EPOCH]
    assert _revision_of(stamps, ["acme/llama-70b"]) != _revision_of(stamps, ["acme/llama-8b"])


def test_the_revision_depends_on_the_served_set_and_not_on_the_row_order():
    """Two resolutions of one table must agree, however the rows came back."""
    stamps = [EPOCH]
    one = _revision_of(stamps, ["acme/llama-70b", "acme/llama-8b"])
    other = _revision_of(stamps, ["acme/llama-8b", "acme/llama-70b"])
    assert one == other
    assert _revision_of(stamps, ["acme/llama-70b", "acme/llama-70b"]) == _revision_of(stamps, ["acme/llama-70b"])


def test_a_stamp_prefers_the_last_write_and_survives_a_missing_row():
    class Row:
        created_at = EPOCH
        updated_at = None

    assert _stamp(None) is None
    assert _stamp(Row()) == EPOCH
    Row.updated_at = EPOCH + dt.timedelta(hours=1)
    assert _stamp(Row()) == Row.updated_at


# --- what the gateway polls ---------------------------------------------------


def test_the_commitment_rides_the_routing_table_the_gateway_already_polls():
    assert "limits" in ResolvedEndpointSchema.__struct_fields__


def test_an_endpoint_with_no_commitment_resolves_to_null_and_not_to_zeros():
    annotation = ResolvedEndpointSchema.__annotations__["limits"]
    assert "None" in str(annotation)


def test_the_routing_table_joins_the_commitment_one_to_one(repo=None):
    text = sql(EndpointRepository(db=None).resolved_query())
    assert "LEFT OUTER JOIN bind_serving_quota ON bind_serving_quota.endpoint_id = demand_endpoint.id" in text


def test_the_resolved_limits_are_built_from_the_quota_row_and_nothing_else():
    quota = FakeQuota(committed_rpm=1_000, committed_tpm=10_000_000, enforced=True)
    limits = declared_limits(quota)
    assert limits.committed_rpm == 1_000
    assert limits.committed_tpm == 10_000_000
    assert limits.enforced is True
    assert "availability_objective" not in ResolvedLimitsSchema.__struct_fields__
    assert "success_rate_objective" not in ResolvedLimitsSchema.__struct_fields__


def test_shadow_mode_is_a_declared_field_and_not_a_phase():
    assert declared_limits(FakeQuota()).enforced is False
    assert "phase" not in ServingQuotaSchema.__struct_fields__
    assert "state" not in ServingQuotaSchema.__struct_fields__


# --- the controller -----------------------------------------------------------


def test_controller_exposes_full_crud_plus_the_resolution():
    handlers = {h.handler_name for h in ServingQuotaRoutes.__dict__.values() if hasattr(h, "handler_name")}
    assert handlers == {
        "list_serving_quotas",
        "create_serving_quota",
        "resolve_serving_quotas",
        "get_serving_quota",
        "update_serving_quota",
        "delete_serving_quota",
    }


def test_the_two_controllers_sit_on_different_paths():
    assert ServingQuotaRoutes.path == "/serving-quotas"
    assert QuotaRoutes.path == "/quotas"


def test_the_enforcement_mode_filter_is_exposed_as_scope():
    parameter = inspect.signature(ServingQuotaRoutes.list_serving_quotas.fn).parameters["limit_scope"]
    assert parameter.default.query == "scope"


def test_update_accepts_if_match_against_the_revision():
    parameter = inspect.signature(ServingQuotaRoutes.update_serving_quota.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


def test_the_resolution_polls_with_if_none_match_and_defaults_to_a_fleet_of_one():
    parameters = inspect.signature(ServingQuotaRoutes.resolve_serving_quotas.fn).parameters
    assert parameters["if_none_match"].default.header == "If-None-Match"
    assert parameters["replicas"].default == 1
    assert parameters["replica_index"].default == 0
    assert set(parameters) >= {"endpoint", "workload"}


def test_the_resolution_is_not_paginated():
    parameters = inspect.signature(ServingQuotaRoutes.resolve_serving_quotas.fn).parameters
    assert "limit" not in parameters
    assert "cursor" not in parameters


def test_no_handler_here_reads_a_measurement():
    for name in (
        "list_serving_quotas",
        "get_serving_quota",
        "create_serving_quota",
        "update_serving_quota",
        "resolve_serving_quotas",
    ):
        source = inspect.getsource(getattr(ServingQuotaRoutes, name).fn)
        assert "metrics" not in source.lower().replace("metrics surface", "")


# --- the service, against a repository that is not a database -----------------


class FakeRepo:
    def __init__(self, rows=(), endpoint=None) -> None:
        self.rows = list(rows)
        self.endpoint = endpoint
        self.added = []
        self.deleted = []

    async def resolved(self, endpoint=None, workload=None):
        return self.rows

    async def endpoint_by_ref(self, ref):
        return self.endpoint


class FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeEndpoint:
    def __init__(self, uid="end_1", name="chat") -> None:
        self.uid = uid
        self.name = name
        self.created_at = EPOCH
        self.updated_at = None
        self.surface = {}


class FakeWorkload:
    def __init__(self, uid) -> None:
        self.uid = uid
        self.created_at = EPOCH
        self.updated_at = None


class FakeRow(tuple):
    """A `Row` is a tuple with labels; the resolution reads it both ways."""

    def __new__(cls, quota, endpoint, project_uid, target, workload):
        row = super().__new__(cls, (quota, endpoint, project_uid, target, workload))
        row.project_uid = project_uid
        return row


def service(rows=(), endpoint=None) -> ServingQuotaService:
    svc = ServingQuotaService.__new__(ServingQuotaService)
    svc.db = FakeDB()
    svc.repo = FakeRepo(rows, endpoint)
    return svc


def quota_row(quota, workloads=("wkl_a",), endpoint=None):
    endpoint = endpoint or FakeEndpoint()
    return [FakeRow(quota, endpoint, "prj_1", None, FakeWorkload(uid)) for uid in workloads]


@pytest.mark.asyncio
async def test_a_reference_that_matches_nothing_resolves_to_an_empty_table_not_a_404():
    table = await service().resolved(endpoint="chat")
    assert table.quotas == []
    assert table.revision == "0-0-1-0"


@pytest.mark.asyncio
async def test_the_resolution_reports_the_fleet_it_was_asked_about():
    quota = FakeQuota(committed_rpm=1_000, created_at=EPOCH, updated_at=None)
    table = await service(quota_row(quota)).resolved(replicas=3, replica_index=2)
    assert table.replicas == 3
    assert table.replica_index == 2
    assert table.quotas[0].replicas == 3
    assert table.quotas[0].replica_index == 2


@pytest.mark.asyncio
async def test_the_resolution_reports_declared_and_effective_side_by_side():
    quota = divided(committed_rpm=1_000, created_at=EPOCH, updated_at=None)
    table = await service(quota_row(quota)).resolved(replicas=3, replica_index=0)
    entry = table.quotas[0]
    assert entry.declared.committed_rpm == 1_000
    assert entry.effective.committed_rpm == 334
    assert entry.starved_replicas == 0


@pytest.mark.asyncio
async def test_a_workload_behind_two_endpoints_gets_two_entries_never_a_sum():
    left = FakeQuota(committed_rpm=1_000, created_at=EPOCH, updated_at=None)
    right = FakeQuota(committed_rpm=400, created_at=EPOCH, updated_at=None)
    rows = quota_row(left, ("wkl_a",), FakeEndpoint("end_1", "chat"))
    rows += quota_row(right, ("wkl_a",), FakeEndpoint("end_2", "chat-eu"))
    table = await service(rows).resolved(workload="summariser")
    assert [q.declared.committed_rpm for q in table.quotas] == [1_000, 400]
    assert [q.endpoint for q in table.quotas] == ["end_1", "end_2"]


@pytest.mark.asyncio
async def test_the_workloads_behind_one_endpoint_are_listed_once_each():
    quota = FakeQuota(committed_rpm=1_000, created_at=EPOCH, updated_at=None)
    rows = quota_row(quota, ("wkl_a", "wkl_b", "wkl_a"))
    table = await service(rows).resolved()
    assert table.quotas[0].workloads == ["wkl_a", "wkl_b"]


@pytest.mark.asyncio
async def test_an_impossible_fleet_is_refused_before_anything_is_read():
    with pytest.raises(Invalid):
        await service().resolved(replicas=2, replica_index=2)


@pytest.mark.asyncio
async def test_a_stale_if_match_is_a_conflict_and_touches_no_field():
    quota = FakeQuota(committed_rpm=1_000, created_at=EPOCH, updated_at=None)
    svc = service()

    async def _one(ref):
        return FakeRow(quota, FakeEndpoint(), "prj_1", None, None)

    svc._one = _one
    with pytest.raises(Conflict):
        await svc.update("sqt_x", ServingQuotaUpdateRequest(committed_rpm=50), if_match="1")
    assert quota.committed_rpm == 1_000
    assert svc.db.commits == 0
