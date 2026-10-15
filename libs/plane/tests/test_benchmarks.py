import datetime as dt
import dataclasses

from types import SimpleNamespace

import pytest
import msgspec

from app.core.ids import CLUSTER
from app.core.ids import DEPLOYMENT
from app.core.ids import BENCHMARK_RUN
from app.core.ids import new_uid
from app.config.settings import Settings
from app.config.settings import load as load_settings
from app.core.exceptions import Invalid
from app.modules.benchmarks import harnesses
from app.modules.benchmarks.follow import DEFAULT_MESSAGES
from app.modules.benchmarks.follow import follows_operation
from app.modules.benchmarks.routes import BenchmarkRoutes
from app.modules.benchmarks.schemas import BenchmarkRunSchema
from app.modules.benchmarks.schemas import BenchmarkProvenanceSchema
from app.modules.benchmarks.schemas import BenchmarkRunCreateRequest
from app.modules.benchmarks.schemas import ServingBenchmarkSummarySchema
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


def test_every_harness_declares_where_its_image_comes_from():
    for harness in harnesses.HARNESSES.values():
        assert bool(harness.pinned_image) != bool(harness.image_setting), harness.name
        assert harness.parameters, harness.name


def test_a_pinned_harness_image_is_a_version_rather_than_floating():
    """A run whose tool moves under it is not comparable to the run before it."""
    for harness in harnesses.HARNESSES.values():
        if harness.pinned_image:
            assert not harness.pinned_image.endswith(":latest"), harness.name
            assert ":" in harness.pinned_image.rsplit("/", 1)[-1], harness.name


def test_every_image_setting_is_a_plane_setting():
    names = {spec.name for spec in dataclasses.fields(Settings)}
    for harness in harnesses.HARNESSES.values():
        if harness.image_setting:
            assert harness.image_setting in names, harness.name


def test_harness_image_settings_load_from_the_environment():
    loaded = load_settings(
        {
            "HANGAR_BENCHMARK_EVALSCOPE_IMAGE": "registry.example/evalscope:1.11.1",
            "HANGAR_BENCHMARK_CONFORMANCE_IMAGE": "registry.example/conformance:0.1.0",
        }
    )
    assert loaded.BENCHMARK_EVALSCOPE_IMAGE == "registry.example/evalscope:1.11.1"
    assert loaded.BENCHMARK_CONFORMANCE_IMAGE == "registry.example/conformance:0.1.0"


def test_a_harness_image_comes_from_its_setting(monkeypatch):
    monkeypatch.setattr(harnesses.settings, "BENCHMARK_EVALSCOPE_IMAGE", "registry.example/evalscope:1.11.1")
    assert harnesses.resolve_image(harnesses.EVALSCOPE, None) == "registry.example/evalscope:1.11.1"


def test_an_unset_harness_image_is_refused(monkeypatch):
    monkeypatch.setattr(harnesses.settings, "BENCHMARK_CONFORMANCE_IMAGE", "")
    with pytest.raises(Invalid) as caught:
        harnesses.resolve_image(harnesses.CONFORMANCE, None)
    assert caught.value.detail == {"harness": "conformance", "setting": "HANGAR_BENCHMARK_CONFORMANCE_IMAGE"}
    assert harnesses.resolve_image(harnesses.CONFORMANCE, "registry.example/conformance:0.1.0")


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


def test_evalscope_reaches_the_wire_as_a_command_the_operator_runs_verbatim():
    """The operator's generic case is generic because the command line is composed here."""
    resolved = harnesses.normalise(
        harnesses.EVALSCOPE,
        {"datasets": "mmlu_pro,gpqa_diamond", "limit": 500, "temperature": 0.0},
        harnesses.DEPLOYMENT,
    )
    flattened = harnesses.wire_parameters(harnesses.EVALSCOPE, resolved, None, "Qwen/Qwen3-32B")
    command = flattened["command"]
    assert command.startswith("evalscope eval")
    assert "--model 'Qwen/Qwen3-32B'" in command
    # EvalScope's --datasets is a space-separated list at 1.11.1, not a comma-joined string.
    assert "--datasets 'mmlu_pro' 'gpqa_diamond'" in command
    assert "--limit 500" in command
    assert "--eval-type openai_api" in command
    # The endpoint is a cluster-local fact and the operator's renderer fills it in.
    # EvalScope treats --api-url as an OpenAI SDK base_url, which expects /v1.
    assert '"$HANGAR_BENCHMARK_ENDPOINT/v1"' in command
    assert flattened["version_command"] == "evalscope --version"


def test_evalscope_packaging_decoding_into_generation_config_json():
    resolved = harnesses.normalise(
        harnesses.EVALSCOPE,
        {"temperature": 0.7, "top_p": 0.95, "max_output_tokens": 8192},
        harnesses.DEPLOYMENT,
    )
    flattened = harnesses.wire_parameters(harnesses.EVALSCOPE, resolved, None, "m")
    command = flattened["command"]
    assert "--generation-config" in command
    assert '"temperature":0.7' in command
    assert '"top_p":0.95' in command
    assert '"max_tokens":8192' in command


def test_evalscope_omits_the_judge_flags_when_no_judge_is_set():
    flattened = harnesses.wire_parameters(
        harnesses.EVALSCOPE, harnesses.normalise(harnesses.EVALSCOPE, {}, harnesses.DEPLOYMENT), None, "m"
    )
    assert "--judge" not in flattened["command"]


def test_evalscope_includes_the_judge_as_one_json_object():
    resolved = harnesses.normalise(
        harnesses.EVALSCOPE,
        {"judge_base_url": "http://judge:8000/v1", "judge_model": "grader"},
        harnesses.DEPLOYMENT,
    )
    flattened = harnesses.wire_parameters(harnesses.EVALSCOPE, resolved, None, "m")
    command = flattened["command"]
    assert "--judge" in command
    assert '"base_url":"http://judge:8000/v1"' in command
    assert '"model":"grader"' in command


def test_evalscope_a_supplied_command_wins_over_composition_whole():
    """A new flag must not need a plane deploy."""
    resolved = harnesses.normalise(
        harnesses.EVALSCOPE,
        {
            "datasets": "mmlu_pro",
            "command": "evalscope eval --model $HANGAR_BENCHMARK_MODEL --api-url $HANGAR_BENCHMARK_ENDPOINT/v1 --datasets mmlu_pro --eval-type openai_api --some-new-flag from-1.12",
        },
        harnesses.DEPLOYMENT,
    )
    flattened = harnesses.wire_parameters(harnesses.EVALSCOPE, resolved, None, "m")
    assert flattened["command"].endswith("--some-new-flag from-1.12")
    assert "$HANGAR_BENCHMARK_ENDPOINT" in flattened["command"]


def test_evalscope_endpoint_model_travels_separately_from_the_served_name():
    """`endpoint_model` is the request body's `model`, not the run's label."""
    flattened = harnesses.wire_parameters(
        harnesses.EVALSCOPE,
        harnesses.normalise(harnesses.EVALSCOPE, {}, harnesses.DEPLOYMENT),
        None,
        model="kimi-k3-site-c",
        endpoint_model="moonshotai/Kimi-K3",
    )
    assert flattened["model"] == "kimi-k3-site-c"
    assert flattened["endpoint_model"] == "moonshotai/Kimi-K3"


def test_evalscope_endpoint_model_is_omitted_when_identical():
    """The operator falls back when it is absent; carrying the same string twice only invites drift."""
    flattened = harnesses.wire_parameters(
        harnesses.EVALSCOPE,
        harnesses.normalise(harnesses.EVALSCOPE, {}, harnesses.DEPLOYMENT),
        None,
        model="m",
        endpoint_model="m",
    )
    assert "endpoint_model" not in flattened


def test_evalscope_endpoints_go_through_shell_quoting():
    """A parameter is attacker-adjacent by the time it reaches a shell; quoting is not optional."""
    resolved = harnesses.normalise(harnesses.EVALSCOPE, {"datasets": "mmlu_pro"}, harnesses.DEPLOYMENT)
    flattened = harnesses.wire_parameters(harnesses.EVALSCOPE, resolved, None, "a'; rm -rf /; echo '")
    assert "rm -rf /" in flattened["command"]
    assert "'\\''" in flattened["command"]


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


def test_every_join_a_run_is_named_by_is_outer(repo):
    """All five outlive-able, and two of them are absent by design.

    An operation is only the errand and a request only the batch it arrived in,
    so both were already outer. The deployment and the cluster joined the list
    when the endpoint level arrived: a run against a public name has neither, and
    an inner join on either would hide exactly the runs the level exists for.

    There is one fewer join than there was, and not because one was relaxed: a
    run names its serving configuration by the digest it snapshotted, which is a
    value and joins to nothing.
    """
    query = sql(repo.get_query(new_uid(BENCHMARK_RUN)))
    assert query.count("LEFT OUTER JOIN") == 5
    assert "JOIN demand_deployment" in query
    assert "JOIN demand_endpoint" in query
    assert " JOIN " not in query.replace("LEFT OUTER JOIN", "")


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
    """A workload is mutable and a deployment is disposable; a joined run would drift off what it measured.

    The serving configuration is named by the digest the run snapshotted, which
    is the one field here that cannot go stale: it is the content of the
    configuration rather than a handle on it, where the id and version it
    replaces named a row that could be edited under the run.
    """
    fields = {field.name: field.type for field in msgspec.structs.fields(BenchmarkRunSchema)}
    assert fields["provenance"] is BenchmarkProvenanceSchema
    snapshot = {field.name for field in msgspec.structs.fields(BenchmarkProvenanceSchema)}
    assert snapshot == {"model", "deployment_generation", "serving_digest", "engine_args", "serving"}


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


def _workload(**engine_args):
    return SimpleNamespace(name="qwen3.8-27b", engine_args=engine_args)


def _artifact(digest="", name="", metadata=None, locations=()):
    return SimpleNamespace(
        digest=digest,
        name=name,
        metadata_=metadata or {},
        locations=[SimpleNamespace(uri=uri) for uri in locations],
    )


def test_the_tokenizer_comes_off_the_artifact_and_not_the_served_name():
    resolved = tokenizer_for(_workload(), _artifact(digest="hf:Qwen/Qwen3-32B"))
    assert resolved == "Qwen/Qwen3-32B"


def test_a_pinned_revision_is_stripped_from_the_tokenizer_reference():
    resolved = tokenizer_for(_workload(), _artifact(digest="hf:Qwen/Qwen3-32B@main"))
    assert resolved == "Qwen/Qwen3-32B"


def test_the_workload_wins_over_the_artifact_when_it_names_a_tokenizer():
    workload = _workload(tokenizer="org/other-tokenizer")
    assert tokenizer_for(workload, _artifact(digest="hf:Qwen/Qwen3-32B")) == "org/other-tokenizer"


def test_a_content_addressed_artifact_falls_back_to_its_repository_shaped_name():
    assert tokenizer_for(_workload(), _artifact(digest="sha256:abc", name="Qwen/Qwen3-32B")) == "Qwen/Qwen3-32B"


def test_nothing_repository_shaped_resolves_to_nothing_rather_than_a_guess():
    assert tokenizer_for(_workload(), _artifact(digest="sha256:abc", name="qwen3.8-27b")) == ""


def test_a_tokenizer_discovery_read_off_the_running_workload_is_used():
    artifact = _artifact(digest="hf:Qwen/Qwen3.8-27B", metadata={"tokenizer": "org/other"})
    assert tokenizer_for(_workload(), artifact) == "org/other"


def test_an_engine_argument_written_the_way_discovery_writes_them_is_read():
    workload = SimpleNamespace(name="qwen3.8-27b", engine_args={"--tokenizer": "org/other"})
    assert tokenizer_for(workload, _artifact(digest="hf:Qwen/Qwen3.8-27B")) == "org/other"


# The operator points `--model` at the artifact's `hf://` source, so that is the repository whose
# tokenizer the engine actually loaded. Reading it from anywhere else is how the two drift.
def test_the_hf_location_the_engine_pulled_from_is_the_tokenizer():
    artifact = _artifact(digest="sha256:abc", locations=["s3://weights/x", "hf://Qwen/Qwen3.8-27B"])
    assert tokenizer_for(_workload(), artifact) == "Qwen/Qwen3.8-27B"


# ---------------------------------------------------------------- the serving rollup


DIGEST = "svg_" + "a" * 64


class _Row:
    def __init__(self, harness, harness_version, summary):
        self.harness = harness
        self.harness_version = harness_version
        self.summary = summary


class _StubRepo:
    def __init__(self, rows):
        self._rows = rows

    async def serving_runs(self, digest):
        return self._rows


def test_the_rollup_reads_only_graded_non_discarded_runs(repo):
    """No join at all: the digest is a value the run snapshotted, not a row it points at."""
    text = sql(repo.serving_runs_query(DIGEST))
    assert "ops_benchmark_run.serving_digest" in text
    assert "ops_benchmark_run.discard = false" in text
    assert "ops_benchmark_run.summary" in text
    assert "ops_benchmark_run.harness" in text
    assert " JOIN " not in text


async def test_serving_summary_takes_the_peak_and_skips_unreadable_runs():
    """Only runs whose harness-and-version adapter resolves a throughput contribute."""
    rows = [
        _Row("aiperf", "0.4.0", {"output_token_throughput_per_user": {"avg": 120.0}}),
        _Row("aiperf", "0.4.0", {"output_token_throughput_per_user": {"avg": 260.0}}),
        _Row("aiperf", None, {"output_token_throughput_per_user": {"avg": 999.0}}),
        _Row("aiperf", "0.4.0", {"nothing_here": True}),
    ]
    svc = BenchmarkService(db=None)
    svc.repo = _StubRepo(rows)
    summary = await svc.serving_summary(DIGEST)
    assert summary.serving_digest == DIGEST
    assert summary.run_count == 4
    assert summary.graded_run_count == 2
    assert summary.peak_output_tokens_per_second == 260.0


async def test_serving_summary_peak_is_none_when_nothing_can_be_read():
    svc = BenchmarkService(db=None)
    svc.repo = _StubRepo([_Row("unknown-harness", "1.0.0", {"x": 1})])
    summary = await svc.serving_summary(DIGEST)
    assert summary.graded_run_count == 0
    assert summary.peak_output_tokens_per_second is None


async def test_an_unmeasured_configuration_is_a_zero_count_and_not_a_miss():
    """A digest is content, not an object, so there is nothing for a 404 to be about.

    The question the rollup answers is how much measurement stands behind a
    configuration, and "none" is an answer. The recipe rollup this replaces was
    addressed by a row id, where absence really was a missing object.
    """
    svc = BenchmarkService(db=None)
    svc.repo = _StubRepo([])
    summary = await svc.serving_summary(DIGEST)
    assert summary.run_count == 0
    assert summary.graded_run_count == 0
    assert summary.peak_output_tokens_per_second is None


def test_the_serving_summary_is_keyed_on_the_digest_and_its_peak_can_be_absent():
    assert set(ServingBenchmarkSummarySchema.__struct_fields__) == {
        "serving_digest",
        "run_count",
        "graded_run_count",
        "peak_output_tokens_per_second",
    }
    annotation = ServingBenchmarkSummarySchema.__annotations__["peak_output_tokens_per_second"]
    assert type(None) in annotation.__args__


def test_the_rollup_is_addressed_by_digest_beside_the_runs():
    methods = {name for name, member in vars(BenchmarkRoutes).items() if hasattr(member, "fn")}
    assert "get_serving_summary" in methods


def test_the_request_filter_is_aliased_away_from_litestars_own_name() -> None:
    """A handler parameter called `request` is handed the Request, not the query value.

    It is never None, so the filter it fed was applied on every call and every
    `GET /v1/benchmarks` answered 500 with `cannot adapt type 'Request' using
    placeholder '%s'`. The bug is invisible in a unit test of the service and
    only shows against a database, which is why it reached production.
    """
    import inspect

    from litestar.params import ParameterKwarg

    from app.modules.benchmarks.routes import BenchmarkRoutes

    handler = BenchmarkRoutes.list_runs
    signature = inspect.signature(getattr(handler, "fn", handler))
    assert "request" not in signature.parameters, "Litestar owns this name on a handler"

    default = signature.parameters["request_uid"].default
    assert isinstance(default, ParameterKwarg)
    assert default.query == "request", "the query key callers use must not change"
