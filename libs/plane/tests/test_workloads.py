"""The workload's own surface: what a caller may state, and how a read is narrowed.

The serving configuration it absorbed from `Recipe` is tested in `test_serving.py`.
What is here is the object (the request and response shapes, the repository's
filters, the routes), including the shapes of the serving block *as an API surface*,
because "can a patch clear the device class" is a question about the request struct,
not about what a validator does with it.
"""

import inspect
import datetime as dt

import pytest
import msgspec

from app.core.ids import ARTIFACT
from app.core.ids import new_uid
from app.db.models import Workload
from app.core.schemas import UNSET
from app.core.exceptions import Invalid
from app.modules.workloads.views import UNITS
from app.modules.workloads.views import _revision
from app.modules.workloads.views import derive_phase
from app.modules.workloads.routes import WorkloadRoutes
from app.modules.workloads.schemas import CacheSchema
from app.modules.workloads.schemas import ShapeSchema
from app.modules.workloads.schemas import OverlaySchema
from app.modules.workloads.schemas import ParallelismSchema
from app.modules.workloads.schemas import WorkloadCreateRequest
from app.modules.workloads.schemas import WorkloadServingUpdate
from app.modules.workloads.schemas import WorkloadUpdateRequest
from app.modules.workloads.schemas import WorkloadServingRequest
from app.modules.workloads.services import KINDS
from app.modules.workloads.services import PHASES
from app.modules.workloads.services import STRATEGIES
from app.modules.policies.repository import SUBJECTS
from app.modules.workloads.repository import WorkloadRepository
from app.modules.workloads.validation import check_scale
from app.modules.workloads.validation import check_overlay


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


def patch(body: bytes, type_=None):
    return msgspec.json.decode(body, type=type_ or WorkloadUpdateRequest)


def decode(body: dict, type_):
    """A body as a client would send it, through the decoder the routes use."""
    return msgspec.json.decode(msgspec.json.encode(body), type=type_)


def a_create_body(**kwargs) -> dict:
    return {"name": "serve", "project": "acme", "serving": msgspec.to_builtins(a_serving())} | kwargs


@pytest.mark.parametrize("field", ["desired_scale", "min_scale", "max_scale"])
def test_scale_is_an_operation_and_not_a_field_write(field):
    with pytest.raises(msgspec.ValidationError, match=field):
        patch(b'{"%s": 3}' % field.encode())


def test_a_workload_cannot_be_moved_between_clusters_by_patch():
    """A deployment is workload × cluster, so moving one is a re-expansion, not a field write.

    The digest is derived from the write and never taken from a caller.
    """
    for body in (b'{"cluster": "iad-1"}', b'{"serving_digest": "sha256:0"}'):
        with pytest.raises(msgspec.ValidationError):
            patch(body)


def test_the_weights_a_workload_serves_cannot_be_repointed_by_a_patch():
    """Repointing at other weights makes every deployment expanded from it mean something else.

    That is a new workload, and it is cheap. `artifact` is therefore absent from the
    patch while every other part of the serving block is present.
    """
    with pytest.raises(msgspec.ValidationError, match="artifact"):
        patch(b'{"artifact": "llama-8b"}', WorkloadServingUpdate)
    data = patch(b'{"engine": "sglang", "shape": {"device_class": "h200"}}', WorkloadServingUpdate)
    assert data.to_dict(exclude_unset=True) == {"engine": "sglang", "shape": ShapeSchema(device_class="h200")}


def test_a_revision_snapshot_decodes_as_a_patch_because_the_two_structs_agree():
    """A revision is stored to be resent, so the patch has to accept everything the read holds.

    `artifact` is the one deliberate difference and is recorded on the revision row instead.
    Any other field added to one struct and not the other silently makes old revisions
    un-reappliable, which is the failure this pins.
    """
    stored = a_serving(overlay=OverlaySchema(pod_spec={"terminationGracePeriodSeconds": 60}))
    body = msgspec.to_builtins(stored)
    artifact = body.pop("artifact")
    reapplied = decode(body, WorkloadServingUpdate)
    assert UNSET not in reapplied.to_dict().values()
    assert decode(msgspec.to_builtins(reapplied) | {"artifact": artifact}, WorkloadServingRequest) == stored


def test_the_serving_configuration_is_declared_on_the_workload_and_patchable():
    """It is stated inline instead of named: there is no shared object to point at."""
    assert decode(a_create_body(), WorkloadCreateRequest).serving == a_serving()
    data = patch(b'{"serving": {"engine_runtime_version": "0.7.0"}}')
    assert data.serving.to_dict(exclude_unset=True) == {"engine_runtime_version": "0.7.0"}
    with pytest.raises(msgspec.ValidationError, match="serving_digest"):
        patch(b'{"serving_digest": "svg_0"}')


def test_a_serving_patch_leaves_absent_fields_unset():
    data = WorkloadServingUpdate(engine_runtime_version="0.7.0")
    assert data.to_dict(exclude_unset=True) == {"engine_runtime_version": "0.7.0"}
    assert WorkloadServingUpdate(quantization=None).to_dict(exclude_unset=True) == {"quantization": None}
    assert WorkloadServingUpdate().to_dict(exclude_unset=True) == {}


def test_device_class_is_required_at_the_request_boundary_too():
    """A shape without a class is not a shape, and the struct says so before a validator does."""
    with pytest.raises(TypeError):
        ShapeSchema()
    with pytest.raises(msgspec.ValidationError, match="device_class"):
        patch(b'{"shape": {"device_count": 2}}', WorkloadServingUpdate)
    with pytest.raises(TypeError):
        WorkloadServingRequest(artifact="a", engine="vllm", engine_runtime_version="0.6", context_length=8192)


def test_device_class_cannot_be_cleared_by_a_patch():
    """The shape replaces whole, and `device_class` is not nullable inside it.

    Sending a shape means stating one: its fields are meaningless apart, so a patch
    that named half of it would leave the rest at its defaults, not at what
    was stored, and the caller could not tell which it got.
    """
    for body in (b'{"shape": null}', b'{"shape": {"device_class": null}}'):
        with pytest.raises(msgspec.ValidationError):
            patch(body, WorkloadServingUpdate)


def test_context_length_lives_on_the_serving_block_and_not_on_a_catalog_entry():
    assert decode(a_create_body(), WorkloadCreateRequest).serving.context_length == 8192
    assert patch(b'{"context_length": 32768}', WorkloadServingUpdate).context_length == 32768
    with pytest.raises(msgspec.ValidationError, match="context_length"):
        decode(a_create_body(context_length=8192), WorkloadCreateRequest)


def test_kv_cache_is_serving_configuration_and_not_the_weight_cache():
    """Per-request attention state, not a declared warm set with observed residency."""
    cache = {"kv_cache_dtype": "fp8", "kv_cache_fraction": 0.9, "prefix_cache_enabled": False}
    assert decode(cache, CacheSchema) == CacheSchema(**cache)
    for weights in ({"warm_set": ["llama-70b"]}, {"resident": True}):
        with pytest.raises(msgspec.ValidationError):
            decode(weights, CacheSchema)


def test_a_workload_that_says_nothing_about_the_prefix_cache_gets_it():
    """The default is a position, not the default a bool gets when nobody picks one.

    vLLM caches prefixes itself, and the operator only renders the off case. A
    false default here meant every workload that never mentioned the field shipped
    --no-enable-prefix-caching.
    """
    assert decode({}, CacheSchema).prefix_cache_enabled is True
    assert a_serving().cache.prefix_cache_enabled is True


def test_the_shape_is_stated_as_one_thing_class_included():
    assert patch(b'{"shape": {"device_class": "h200"}}', WorkloadServingUpdate).shape.device_class == "h200"
    with pytest.raises(msgspec.ValidationError, match="device_class"):
        patch(b'{"device_class": "h200"}', WorkloadServingUpdate)
    with pytest.raises(msgspec.ValidationError, match="device_class"):
        decode(a_create_body(device_class="h200"), WorkloadCreateRequest)


def test_the_topology_pair_sits_with_the_shape_it_describes():
    """One replica's layout, declared once. It used to be a recipe's requirement and a
    nullable override of it on the workload, so a null meant "the recipe decides"."""
    pair = {"requires_single_topology_domain": True, "topology_domain_kind": "nvlink"}
    shape = decode({"device_class": "h100"} | pair, ShapeSchema)
    assert (shape.requires_single_topology_domain, shape.topology_domain_kind) == (True, "nvlink")
    with pytest.raises(msgspec.ValidationError, match="requires_single_topology_domain"):
        decode(a_create_body(requires_single_topology_domain=True), WorkloadCreateRequest)
    assert ShapeSchema(device_class="h100").requires_single_topology_domain is False


def test_the_overlay_is_a_passthrough_on_every_edge_of_the_surface():
    """What the plane does not model still has to be sayable and patchable, inside its allowlist."""
    overlay = {"pod_spec": {"terminationGracePeriodSeconds": 60}, "dynamo": {"annotations": {"team": "search"}}}
    body = a_create_body()
    body["serving"]["overlay"] = overlay
    assert decode(body, WorkloadCreateRequest).serving.overlay == OverlaySchema(**overlay)
    assert decode({"overlay": overlay}, WorkloadServingUpdate).overlay == OverlaySchema(**overlay)
    check_overlay(overlay["pod_spec"], overlay["dynamo"])
    with pytest.raises(Invalid, match="hostNetwork"):
        check_overlay({"hostNetwork": True}, {})


def test_an_absent_overlay_defaults_to_an_empty_one():
    data = a_serving()
    assert data.overlay == OverlaySchema()
    assert (data.overlay.pod_spec, data.overlay.dynamo) == ({}, {})
    assert data.parallelism == ParallelismSchema(tensor=1, pipeline=1, expert=1)


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


def test_the_served_weights_are_loaded_with_the_page_and_not_labelled_field_by_field(repo):
    """The read needs the artifact entity, not a column of it.

    The served model name and the serving digest are both resolved from the whole
    object, so a label per field they read is one more thing to keep in step every
    time that order changes. `Workload.artifact` is `lazy="noload"`, so the page
    loads it explicitly instead of one round trip per row.
    """
    query = repo.list_query(limit=10)
    assert "demand_artifact" not in sql(query)
    loads = [(load.path[1], load.strategy) for option in query._with_options for load in option.context]
    assert loads == [(Workload.artifact.property, (("lazy", "selectin"),))]


def test_the_weights_are_named_inside_the_serving_block():
    """One place a workload says which weights it serves, and it is where the rest of the
    configuration is. `artifact` is a filter on the list because "which workloads serve
    these weights" is a question, not because the id is declared twice."""
    assert decode(a_create_body(), WorkloadCreateRequest).serving.artifact == "llama-70b"
    with pytest.raises(msgspec.ValidationError, match="artifact"):
        decode(a_create_body(artifact="llama-70b"), WorkloadCreateRequest)


def test_label_selector_becomes_a_predicate_per_key(repo):
    """One `->>` comparison per key, so two labels narrow the match instead of widening it."""
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


@pytest.mark.parametrize("kind", ["task", "gang"])
async def test_a_kind_no_operator_renders_is_refused_at_creation(kind):
    from app.modules.workloads.schemas import ShapeSchema
    from app.modules.workloads.schemas import WorkloadCreateRequest
    from app.modules.workloads.schemas import WorkloadServingRequest
    from app.modules.workloads.services import WorkloadService

    data = WorkloadCreateRequest(
        serving=WorkloadServingRequest(
            artifact="llama", engine="vllm", engine_runtime_version="1.0", shape=ShapeSchema(device_class="h100")
        ),
        name="batch",
        project="core",
        kind=kind,
    )
    with pytest.raises(Invalid, match="not supported yet"):
        await WorkloadService(db=None).create(data)


class _Driver(Exception):
    def __init__(self, constraint_name):
        super().__init__(f"duplicate key value violates unique constraint {constraint_name!r}")
        self.constraint_name = constraint_name


def _integrity(constraint_name):
    from sqlalchemy.exc import IntegrityError

    orig = Exception("wrapped")
    orig.__cause__ = _Driver(constraint_name)
    return IntegrityError("insert", {}, orig)


def test_a_policy_name_collision_is_reported_as_the_policy_not_the_workload():
    from app.modules.workloads.services import _conflict

    conflict = _conflict(_integrity("bind_policy_name_key"), "llama", "core", "wkl_abc-placement")
    assert "policy named 'wkl_abc-placement'" in str(conflict)
    assert "workload" in str(_conflict(_integrity("uq_demand_workload_project_name"), "llama", "core"))
    assert "deployment named 'llama'" in str(_conflict(_integrity("uq_demand_deployment_cluster_name"), "llama", None))


def test_the_constraint_name_is_found_in_the_message_when_the_driver_carries_no_attribute():
    from sqlalchemy.exc import IntegrityError

    from app.modules.workloads.services import violated

    exc = IntegrityError("insert", {}, Exception('violates unique constraint "bind_policy_name_key"'))
    assert violated(exc) == "bind_policy_name_key"


def test_the_placement_shape_moves_with_what_placement_reads_and_nothing_else():
    from types import SimpleNamespace

    from app.db.models import WorkloadRole
    from app.modules.workloads.services import placement_shape

    def workload(**kw):
        base = {
            "device_class": "h100",
            "device_count": 8,
            "device_memory_mib_min": 0,
            "tensor_parallel": 8,
            "requires_single_topology_domain": False,
            "topology_domain_kind": None,
            "capabilities": {},
            "cpu_cores": 8,
        }
        return SimpleNamespace(**(base | kw))

    same = placement_shape(workload(), ())
    assert placement_shape(workload(capabilities={"tool_calling_supported": True}, cpu_cores=16), ()) == same
    assert placement_shape(workload(device_count=16), ()) != same
    assert placement_shape(workload(device_class="h200"), ()) != same
    assert placement_shape(workload(tensor_parallel=4), ()) != same
    roles = [WorkloadRole(role="prefill", weight=1), WorkloadRole(role="decode", weight=2)]
    heavier = [WorkloadRole(role="prefill", weight=1), WorkloadRole(role="decode", weight=3)]
    assert placement_shape(workload(), roles) != placement_shape(workload(), heavier)


class _Clusters:
    def __init__(self, *uids):
        from types import SimpleNamespace

        self.known = {uid: SimpleNamespace(uid=f"clu_{uid}") for uid in uids}

    async def cluster_by_ref(self, ref):
        return self.known.get(ref)


async def test_cluster_floors_are_keyed_by_cluster_id_and_refuse_what_they_cannot_honour():
    from app.core.exceptions import NotFound
    from app.modules.workloads.services import WorkloadService

    service = WorkloadService(db=None)
    service.repo = _Clusters("iad", "sfo")
    assert await service._cluster_floors({"iad": 2, "sfo": 0}) == {"clu_iad": 2, "clu_sfo": 0}
    with pytest.raises(NotFound):
        await service._cluster_floors({"nowhere": 1})
    with pytest.raises(Invalid):
        await service._cluster_floors({"iad": -1})


def test_cluster_floors_must_be_whole_numbers():
    import msgspec

    from app.modules.workloads.schemas import WorkloadUpdateRequest

    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"cluster_floors": {"iad": "two"}}, WorkloadUpdateRequest)
