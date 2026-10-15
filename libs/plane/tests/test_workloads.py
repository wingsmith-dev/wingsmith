"""The workload's own surface: what a caller may state, and how a read is narrowed.

The serving configuration it absorbed from `Recipe` is tested in `test_serving.py`.
What is here is the object — the request and response shapes, the repository's
filters, the routes — including the shapes of the serving block *as an API surface*,
because "can a patch clear the device class" is a question about the request struct
rather than about what a validator does with it.
"""

import inspect
import datetime as dt

import pytest
import msgspec

from app.core.ids import ARTIFACT
from app.core.ids import new_uid
from app.core.exceptions import Invalid
from app.modules.workloads.views import UNITS
from app.modules.workloads.views import _revision
from app.modules.workloads.views import derive_phase
from app.modules.workloads.routes import WorkloadRoutes
from app.modules.workloads.schemas import CacheSchema
from app.modules.workloads.schemas import ShapeSchema
from app.modules.workloads.schemas import OverlaySchema
from app.modules.workloads.schemas import WorkloadSchema
from app.modules.workloads.schemas import ParallelismSchema
from app.modules.workloads.schemas import WorkloadCreateRequest
from app.modules.workloads.schemas import WorkloadServingSchema
from app.modules.workloads.schemas import WorkloadServingUpdate
from app.modules.workloads.schemas import WorkloadUpdateRequest
from app.modules.workloads.schemas import WorkloadServingRequest
from app.modules.workloads.services import KINDS
from app.modules.workloads.services import PHASES
from app.modules.workloads.services import STRATEGIES
from app.modules.policies.repository import SUBJECTS
from app.modules.workloads.repository import WorkloadRepository
from app.modules.workloads.validation import check_scale


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def a_serving(**kwargs) -> WorkloadServingRequest:
    """The smallest serving block a create is allowed to carry."""
    fields = {
        "artifact": "llama-70b",
        "engine": "vllm",
        "engine_runtime_version": "0.6.0",
        "context_length": 8192,
        "shape": ShapeSchema(device_class="h100"),
    }
    return WorkloadServingRequest(**(fields | kwargs))


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
    data = WorkloadCreateRequest(serving=a_serving(), name="serve", project="acme")
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


def test_a_workload_cannot_be_moved_between_clusters_by_patch():
    """A deployment is workload × cluster, so moving one is a re-expansion, not a field write."""
    fields = WorkloadUpdateRequest.__struct_fields__
    assert "cluster" not in fields
    assert "serving_digest" not in fields, "the digest is derived from the write, never taken from a caller"


def test_the_weights_a_workload_serves_cannot_be_repointed_by_a_patch():
    """Repointing at other weights makes every deployment expanded from it mean something else.

    That is a new workload, and it is cheap. `artifact` is therefore absent from the
    patch while every other part of the serving block is present.
    """
    fields = WorkloadServingUpdate.__struct_fields__
    assert "artifact" not in fields
    assert "engine" in fields and "shape" in fields


def test_a_revision_snapshot_decodes_as_a_patch_because_the_two_structs_agree():
    """A revision is stored to be resent, so the patch has to accept everything the read holds.

    `artifact` is the one deliberate difference and is recorded on the revision row instead.
    Any other field added to one struct and not the other silently makes old revisions
    un-reappliable, which is the failure this pins.
    """
    request = set(WorkloadServingRequest.__struct_fields__)
    update = set(WorkloadServingUpdate.__struct_fields__)
    assert request - update == {"artifact"}
    assert update - request == set()


def test_the_serving_configuration_is_declared_on_the_workload_and_patchable():
    """It is stated inline rather than named: there is no shared object to point at."""
    assert "serving" in WorkloadCreateRequest.__struct_fields__
    assert "serving" in WorkloadUpdateRequest.__struct_fields__
    assert WorkloadCreateRequest.__annotations__["serving"] is WorkloadServingRequest
    assert "serving_digest" in WorkloadSchema.__struct_fields__
    assert WorkloadSchema.__annotations__["serving"] is WorkloadServingSchema


def test_a_serving_patch_leaves_absent_fields_unset():
    data = WorkloadServingUpdate(engine_runtime_version="0.7.0")
    assert data.to_dict(exclude_unset=True) == {"engine_runtime_version": "0.7.0"}
    assert WorkloadServingUpdate(quantization=None).to_dict(exclude_unset=True) == {"quantization": None}
    assert WorkloadServingUpdate().to_dict(exclude_unset=True) == {}


def test_device_class_is_required_at_the_request_boundary_too():
    """A shape without a class is not a shape, and the struct says so before a validator does."""
    assert "device_class" in ShapeSchema.__struct_fields__
    with pytest.raises(TypeError):
        ShapeSchema()
    with pytest.raises(TypeError):
        WorkloadServingRequest(artifact="a", engine="vllm", engine_runtime_version="0.6", context_length=8192)


def test_device_class_cannot_be_cleared_by_a_patch():
    """The shape replaces whole, and `device_class` is not nullable inside it.

    Sending a shape means stating one: its fields are meaningless apart, so a patch
    that named half of it would leave the rest at its defaults rather than at what
    was stored, and the caller could not tell which it got.
    """
    assert WorkloadServingUpdate.__annotations__["shape"] == ShapeSchema | msgspec.UnsetType
    assert ShapeSchema.__annotations__["device_class"] is str


def test_context_length_lives_on_the_serving_block_and_not_on_a_catalog_entry():
    assert "context_length" in WorkloadServingSchema.__struct_fields__
    assert "context_length" in WorkloadServingRequest.__struct_fields__


def test_kv_cache_is_serving_configuration_and_not_the_weight_cache():
    """Per-request attention state, not a declared warm set with observed residency."""
    fields = set(CacheSchema.__struct_fields__)
    assert fields == {"kv_cache_dtype", "kv_cache_fraction", "prefix_cache_enabled", "prefix_cache_config"}
    assert not [f for f in WorkloadServingSchema.__struct_fields__ if "warm" in f or "resident" in f]


def test_a_workload_that_says_nothing_about_the_prefix_cache_gets_it():
    """The default is a position, not the default a bool gets when nobody picks one.

    vLLM caches prefixes itself, and the operator only renders the off case. A
    false default here meant every workload that never mentioned the field shipped
    --no-enable-prefix-caching.
    """
    field = {f.name: f for f in msgspec.structs.fields(CacheSchema)}["prefix_cache_enabled"]
    assert field.default is True


def test_the_shape_is_returned_as_one_thing_class_included():
    assert "device_class" in ShapeSchema.__struct_fields__
    assert "device_class" not in WorkloadServingSchema.__struct_fields__
    assert "device_class" not in WorkloadSchema.__struct_fields__


def test_the_topology_pair_sits_with_the_shape_it_describes():
    """One replica's layout, declared once. It used to be a recipe's requirement and a
    nullable override of it on the workload, so a null meant "the recipe decides"."""
    assert {"requires_single_topology_domain", "topology_domain_kind"} <= set(ShapeSchema.__struct_fields__)
    assert not {"requires_single_topology_domain", "topology_domain_kind"} & set(WorkloadSchema.__struct_fields__)
    assert ShapeSchema(device_class="h100").requires_single_topology_domain is False


def test_the_overlay_is_a_passthrough_on_every_edge_of_the_surface():
    """What the plane does not model still has to be sayable, readable, patchable."""
    assert set(OverlaySchema.__struct_fields__) == {"pod_spec", "dynamo"}
    assert "overlay" in WorkloadServingSchema.__struct_fields__
    assert "overlay" in WorkloadServingRequest.__struct_fields__
    assert "overlay" in WorkloadServingUpdate.__struct_fields__


def test_an_absent_overlay_defaults_to_an_empty_one():
    data = a_serving()
    assert data.overlay == OverlaySchema()
    assert (data.overlay.pod_spec, data.overlay.dynamo) == ({}, {})
    assert data.parallelism == ParallelismSchema(tensor=1, pipeline=1, expert=1)


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
    assert WorkloadCreateRequest(serving=a_serving(), name="n", project="p").strategy == "pack"


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


def test_the_served_weights_are_loaded_with_the_page_and_not_labelled_field_by_field(repo):
    """The read needs the artifact entity, not a column of it.

    The served model name and the serving digest are both resolved from the whole
    object, so a label per field they read is one more thing to keep in step every
    time that order changes. `Workload.artifact` is `lazy="noload"`, so the page
    loads it explicitly rather than one round trip per row.
    """
    query = sql(repo.list_query(limit=10))
    assert "demand_artifact" not in query
    assert "selectinload(Workload.artifact)" in inspect.getsource(WorkloadRepository)


def test_the_artifact_tag_is_a_nullable_addition_that_replaces_nothing():
    fields = WorkloadSchema.__struct_fields__
    assert "artifact_tag" in fields
    assert WorkloadSchema.__annotations__["artifact_tag"] == (str | None)
    assert "recipe_id" not in fields and "recipe_name" not in fields


def test_the_weights_are_named_inside_the_serving_block():
    """One place a workload says which weights it serves, and it is where the rest of the
    configuration is. `artifact` is a filter on the list because "which workloads serve
    these weights" is a question, not because the id is declared twice."""
    assert "artifact_id" in WorkloadServingSchema.__struct_fields__
    assert "artifact" in WorkloadServingRequest.__struct_fields__
    assert "artifact_id" not in WorkloadSchema.__struct_fields__


def test_label_selector_becomes_a_predicate_per_key(repo):
    """One `->>` comparison per key, so two labels narrow rather than widen."""
    text = sql(repo.list_query(limit=10, labels={"tier": "gold", "team": "search"}))
    assert text.count("demand_workload.labels ->>") == 2
    assert "->>" not in sql(repo.list_query(limit=10))


def test_list_query_filters_compile_into_one_statement(repo):
    text = sql(
        repo.list_query(
            limit=10,
            after_seq=41,
            project="search",
            kind="service",
            artifact="llama-70b",
            serving_digest="svg_" + "a" * 64,
            device_class="h100",
        )
    )
    assert "demand_workload.seq > " in text
    assert "demand_artifact.name = " in text
    assert "demand_workload.kind = " in text
    assert "demand_workload.serving_digest = " in text
    assert "demand_workload.device_class = " in text
    assert "ORDER BY demand_workload.seq ASC" in text


def test_the_digest_filter_is_an_exact_equality_and_never_a_prefix(repo):
    """A prefix of a hash means nothing.

    This is the replacement for `matchExistingRecipe`, which compared eight fields in
    the browser to decide whether an existing recipe could be reused. "Does this
    configuration already exist" is one indexed equality test now, asked of the plane.
    """
    text = sql(repo.list_query(limit=10, serving_digest="svg_" + "a" * 64))
    assert "demand_workload.serving_digest = " in text
    assert "LIKE" not in text.upper()


def test_artifact_filter_resolves_an_id_against_uid_not_name(repo):
    assert "demand_artifact.uid = " in sql(repo.list_query(limit=10, artifact=new_uid(ARTIFACT)))


def test_list_query_over_fetches_one_row_for_the_cursor(repo):
    assert repo.list_query(limit=10).compile().params["param_1"] == 11


def test_get_query_resolves_a_name_or_an_id(repo):
    assert "demand_workload.name = " in sql(repo.get_query("llama-prod"))
    assert "demand_workload.uid = " in sql(repo.get_query("wkl_01j9x2wwwwwwwwwwwwwwwwwwww"))


def test_class_filter_is_exposed_as_class_not_device_class():
    """Spelled the way every other list in the plane spells it."""
    parameter = inspect.signature(WorkloadRoutes.list_workloads.fn).parameters["device_class"]
    assert parameter.default.query == "class"


def test_the_list_offers_the_filters_a_person_actually_asks_for():
    """`class` answers "what is asking for H100s", which nothing could answer while the
    shape lived on a separate object. `serving_digest` and `artifact` together are
    "which workloads serve these weights, and which of them are the same fit"."""
    parameters = set(inspect.signature(WorkloadRoutes.list_workloads.fn).parameters)
    assert {"artifact", "serving_digest", "device_class", "cluster", "project", "kind", "phase"} <= parameters
    assert "recipe" not in parameters


def test_conditional_update_reads_if_match_from_the_header():
    parameter = inspect.signature(WorkloadRoutes.update_workload.fn).parameters["if_match"]
    assert parameter.default.header == "If-Match"


def test_revision_is_derived_from_the_last_write_not_stored():
    assert _revision(FakeWorkload(), []) == str(int(EPOCH.timestamp() * 1_000_000))


def test_placement_rollup_reads_only_live_allocations_and_devices(repo):
    query = sql(repo.placement_query("some-id"))
    assert "bind_allocation" in query
    assert "bind_allocation_device" in query
    assert "supply_node" in query
    assert query.count("released_at IS NULL") == 2


def test_placement_rollup_carries_no_serving_telemetry():
    from app.modules.workloads.schemas import WorkloadPlacementSchema
    from app.modules.workloads.schemas import WorkloadPlacementNodeSchema
    from app.modules.workloads.schemas import WorkloadReplicaPlacementSchema

    forbidden = {"latency", "throughput", "utilization", "tokens", "requests", "temperature", "memory"}
    for struct in (WorkloadPlacementSchema, WorkloadPlacementNodeSchema, WorkloadReplicaPlacementSchema):
        assert not [f for f in struct.__struct_fields__ if any(word in f for word in forbidden)], struct


def test_placement_is_a_read_only_route():
    names = {n for n, _ in inspect.getmembers(WorkloadRoutes)}
    assert "workload_placement" in names
