import datetime as dt

from types import SimpleNamespace

import pytest
import msgspec

from app.core.ids import CLUSTER
from app.core.ids import DEPLOYMENT
from app.core.ids import BENCHMARK_RUN
from app.core.ids import new_uid
from app.core.exceptions import Invalid
from app.modules.benchmarks import harnesses
from app.modules.benchmarks.follow import DEFAULT_MESSAGES
from app.modules.benchmarks.follow import follows_operation
from app.modules.benchmarks.routes import BenchmarkRoutes
from app.modules.benchmarks.schemas import BenchmarkRunSchema
from app.modules.benchmarks.schemas import BenchmarkProvenanceSchema
from app.modules.benchmarks.schemas import BenchmarkRunCreateRequest
from app.modules.benchmarks.services import OPERATION_TYPE
from app.modules.benchmarks.services import BenchmarkService
from app.modules.benchmarks.services import tokenizer_for
from app.modules.operations.services import TYPES as OPERATION_TYPES
from app.modules.benchmarks.repository import BenchmarkRepository


EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def repo():
    return BenchmarkRepository(db=None)


def sql(query) -> str:
    return str(query)


# ---------------------------------------------------------------- harnesses


def test_defaults_come_from_the_harness_when_nothing_is_supplied():
    resolved = harnesses.normalise(harnesses.AIPERF, {})
    assert resolved["concurrency"] == 10
    assert resolved["request_count"] == 100
    assert resolved["streaming"] is True


def test_a_flag_the_harness_does_not_accept_is_refused_here():
    """A typo must be a 400 at creation, not a job that fails in a cluster ten minutes later."""
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.AIPERF, {"concurrancy": 10})


def test_an_unknown_harness_is_refused():
    with pytest.raises(Invalid):
        harnesses.resolve("wrk")


def test_form_posted_strings_are_coerced_to_the_declared_type():
    resolved = harnesses.normalise(harnesses.AIPERF, {"concurrency": "32", "streaming": "false"})
    assert resolved["concurrency"] == 32
    assert resolved["streaming"] is False


def test_a_value_that_cannot_be_coerced_is_refused_rather_than_defaulted():
    """Silently defaulting would produce a run that measured something nobody asked for."""
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.AIPERF, {"concurrency": "lots"})
    with pytest.raises(Invalid):
        harnesses.normalise(harnesses.AIPERF, {"concurrency": -4})


def test_wire_parameters_are_strings_go_can_parse():
    """`strconv.ParseBool` does not read Python's capitalised `True`."""
    flattened = harnesses.wire_parameters(harnesses.AIPERF, harnesses.normalise(harnesses.AIPERF, {}))
    assert flattened["streaming"] == "true"
    assert flattened["harness"] == "aiperf"
    assert flattened["image"] == harnesses.AIPERF.image
    assert all(isinstance(value, str) for value in flattened.values())


def test_every_harness_declares_the_image_that_carries_it():
    for harness in harnesses.HARNESSES.values():
        assert harness.image, harness.name
        assert harness.parameters, harness.name


def test_every_harness_image_is_pinned_rather_than_floating():
    """A run whose tool moves under it is not comparable to the run before it."""
    for harness in harnesses.HARNESSES.values():
        assert not harness.image.endswith(":latest"), harness.name
        assert ":" in harness.image.rsplit("/", 1)[-1], harness.name


def test_an_omitted_image_falls_back_to_the_harness_pin():
    assert harnesses.resolve_image(harnesses.AIPERF, None) == harnesses.AIPERF.image
    assert harnesses.resolve_image(harnesses.AIPERF, "") == harnesses.AIPERF.image
    assert harnesses.resolve_image(harnesses.AIPERF, "   ") == harnesses.AIPERF.image


def test_a_supplied_image_overrides_the_harness_pin():
    override = "nvcr.io/nvidia/ai-dynamo/aiperf:0.11.0"
    assert harnesses.resolve_image(harnesses.AIPERF, f"  {override} ") == override


def test_an_image_that_cannot_be_a_reference_is_refused_here():
    with pytest.raises(Invalid):
        harnesses.resolve_image(harnesses.AIPERF, "aiperf latest")
    with pytest.raises(Invalid):
        harnesses.resolve_image(harnesses.AIPERF, "x" * 513)


def test_the_overridden_image_is_what_reaches_the_wire():
    override = "nvcr.io/nvidia/ai-dynamo/aiperf:0.11.0"
    flattened = harnesses.wire_parameters(harnesses.AIPERF, harnesses.normalise(harnesses.AIPERF, {}), override)
    assert flattened["image"] == override


# ---------------------------------------------------------------- the operation behind a run


def test_the_run_is_carried_by_a_registered_operation_type():
    """A benchmark reuses the operation machinery rather than growing a second lifecycle."""
    assert OPERATION_TYPE in OPERATION_TYPES
    prefix, wire_kind = OPERATION_TYPES[OPERATION_TYPE]
    assert prefix == DEPLOYMENT
    assert wire_kind == "benchmark"


# ---------------------------------------------------------------- reads


def test_runs_are_addressed_by_id_and_never_by_name():
    """A measurement has no name; what people reach for it by is the deployment, which is a filter."""
    query = sql(BenchmarkRepository(db=None).get_query(new_uid(BENCHMARK_RUN)))
    assert "ops_benchmark_run.uid" in query
    assert "ops_benchmark_run.name" not in query


@pytest.mark.asyncio
async def test_a_reference_that_is_not_a_run_id_is_not_looked_up(repo):
    assert await repo.get(new_uid(DEPLOYMENT)) is None
    assert await repo.get("not-an-id") is None


def test_the_list_is_newest_first(repo):
    query = sql(repo.list_query(limit=10))
    assert "ORDER BY ops_benchmark_run.seq DESC" in query


def test_the_deployment_filter_accepts_an_id_or_a_name(repo):
    assert "demand_deployment.uid" in sql(repo.list_query(limit=10, deployment=new_uid(DEPLOYMENT)))
    assert "demand_deployment.name" in sql(repo.list_query(limit=10, deployment="serve"))


def test_the_cluster_filter_accepts_an_id_or_a_name(repo):
    assert "supply_cluster.uid" in sql(repo.list_query(limit=10, cluster=new_uid(CLUSTER)))
    assert "supply_cluster.name" in sql(repo.list_query(limit=10, cluster="iad-1"))


def test_a_run_names_the_recipe_and_operation_by_outer_join(repo):
    """Both outlive-able: a recipe may be deleted and an operation is only the errand."""
    query = sql(repo.get_query(new_uid(BENCHMARK_RUN)))
    assert query.count("LEFT OUTER JOIN") == 2


@pytest.mark.asyncio
async def test_an_unknown_harness_filter_is_refused_before_the_query():
    with pytest.raises(Invalid):
        await BenchmarkService(db=None).list(harness="wrk")


@pytest.mark.asyncio
async def test_an_unknown_phase_filter_is_refused_before_the_query():
    with pytest.raises(Invalid):
        await BenchmarkService(db=None).list(phase="finished")


# ---------------------------------------------------------------- the shape of the surface


def test_the_create_request_defaults_to_aiperf_and_no_parameters():
    data = BenchmarkRunCreateRequest(deployment="serve")
    assert data.harness == harnesses.DEFAULT_HARNESS
    assert data.parameters == {}


def test_the_harness_catalogue_is_served_by_the_plane():
    """The console renders its form from this; a second copy of the table is a second answer."""
    served = BenchmarkService(db=None).harnesses()
    assert {harness.name for harness in served} == set(harnesses.HARNESSES)
    aiperf = next(harness for harness in served if harness.name == "aiperf")
    assert {p.name for p in aiperf.parameters} == {p.name for p in harnesses.AIPERF.parameters}


def test_there_is_no_update_and_no_delete_route():
    """A measurement is not editable, and deleting one throws away the history worth keeping."""
    methods = {name for name, member in vars(BenchmarkRoutes).items() if hasattr(member, "fn")}
    assert "update_run" not in methods
    assert "delete_run" not in methods
    assert {"list_runs", "create_run", "get_run", "list_harnesses"} <= methods


def test_the_result_is_carried_as_an_opaque_dict():
    """No normalised metric columns: that schema is deferred until two harnesses have been read."""
    fields = {field.name: field.type for field in msgspec.structs.fields(BenchmarkRunSchema)}
    assert fields["summary"] == (dict | None)
    assert "ttft_p50" not in fields
    assert "throughput" not in fields
    assert "output_token_throughput" not in fields


def test_provenance_is_snapshotted_onto_the_run():
    """A recipe is mutable and a deployment is disposable; a joined run would drift off what it measured."""
    fields = {field.name: field.type for field in msgspec.structs.fields(BenchmarkRunSchema)}
    assert fields["provenance"] is BenchmarkProvenanceSchema
    snapshot = {field.name for field in msgspec.structs.fields(BenchmarkProvenanceSchema)}
    assert snapshot == {"model", "deployment_generation", "recipe_id", "recipe_version", "engine_args"}


# ---------------------------------------------------------------- a run whose operation ended


def test_a_terminal_operation_only_touches_a_run_that_is_still_open():
    """A run that already reported was written by the thing that did the work.

    Its answer has to survive an operation frame arriving after it, which is why
    the update is guarded on the open phases rather than on the phase differing.
    """
    query = str(follows_operation("opr_x", "failed"))
    assert "ops_benchmark_run.phase IN" in query
    assert "ops_benchmark_run.operation_id IN" in query


def test_a_run_is_matched_through_its_operation_and_not_passed_down():
    """Most operations carry no run, and neither caller has one in hand to name."""
    assert "ops_operation.uid" in str(follows_operation("opr_x", "cancelled"))


@pytest.mark.parametrize("phase", ["cancelled", "failed", "timed_out", "succeeded"])
def test_every_terminal_phase_leaves_a_reason_on_the_run(phase):
    """`succeeded` reads like a contradiction and is the point.

    An operation that succeeded while its run never reported means the executor
    finished and sent nothing; a run left saying `succeeded` with an empty
    summary would be indistinguishable from a measurement of nothing.
    """
    assert DEFAULT_MESSAGES[phase]


def test_a_failure_message_from_the_cluster_wins_over_the_default():
    """The cluster said why. The default exists for when nothing did."""
    query = follows_operation("opr_x", "failed", "this operator is read-only")
    assert query.compile().params["error"] == "this operator is read-only"


# ---------------------------------------------------------------- tokenizer


def _recipe(**engine_args):
    return SimpleNamespace(name="qwen3.8-27b", engine_args=engine_args)


def _artifact(digest="", name="", metadata=None, locations=()):
    return SimpleNamespace(
        digest=digest,
        name=name,
        metadata_=metadata or {},
        locations=[SimpleNamespace(uri=uri) for uri in locations],
    )


def test_the_tokenizer_comes_off_the_artifact_and_not_the_served_name():
    resolved = tokenizer_for(_recipe(), _artifact(digest="hf:Qwen/Qwen3-32B"))
    assert resolved == "Qwen/Qwen3-32B"


def test_a_pinned_revision_is_stripped_from_the_tokenizer_reference():
    resolved = tokenizer_for(_recipe(), _artifact(digest="hf:Qwen/Qwen3-32B@main"))
    assert resolved == "Qwen/Qwen3-32B"


def test_the_recipe_wins_over_the_artifact_when_it_names_a_tokenizer():
    recipe = _recipe(tokenizer="org/other-tokenizer")
    assert tokenizer_for(recipe, _artifact(digest="hf:Qwen/Qwen3-32B")) == "org/other-tokenizer"


def test_a_content_addressed_artifact_falls_back_to_its_repository_shaped_name():
    assert tokenizer_for(_recipe(), _artifact(digest="sha256:abc", name="Qwen/Qwen3-32B")) == "Qwen/Qwen3-32B"


def test_nothing_repository_shaped_resolves_to_nothing_rather_than_a_guess():
    assert tokenizer_for(_recipe(), _artifact(digest="sha256:abc", name="qwen3.8-27b")) == ""


def test_a_tokenizer_discovery_read_off_the_running_workload_is_used():
    artifact = _artifact(digest="hf:Qwen/Qwen3.8-27B", metadata={"tokenizer": "org/other"})
    assert tokenizer_for(_recipe(), artifact) == "org/other"


def test_an_engine_argument_written_the_way_discovery_writes_them_is_read():
    recipe = SimpleNamespace(name="qwen3.8-27b", engine_args={"--tokenizer": "org/other"})
    assert tokenizer_for(recipe, _artifact(digest="hf:Qwen/Qwen3.8-27B")) == "org/other"


# The operator points `--model` at the artifact's `hf://` source, so that is the repository whose
# tokenizer the engine actually loaded. Reading it from anywhere else is how the two drift.
def test_the_hf_location_the_engine_pulled_from_is_the_tokenizer():
    artifact = _artifact(digest="sha256:abc", locations=["s3://weights/x", "hf://Qwen/Qwen3.8-27B"])
    assert tokenizer_for(_recipe(), artifact) == "Qwen/Qwen3.8-27B"
