"""Reading a Dynamo/vLLM workload as a serving block.

The specs here are copied from what the two inference clusters actually report, not
invented: a parser for a format we control nothing about is only worth what it does on the
real thing.
"""

from app.modules.discovered.derive import derive
from app.modules.discovered.derive import parse_args
from app.modules.discovered.derive import engine_runtime_from_image


QWEN = {
    "image": "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1",
    "command": ["python3", "-m", "dynamo.vllm"],
    "args": [
        "--model",
        "Qwen/Qwen3.8-27B",
        "--served-model-name",
        "qwen3.8-27b",
        "--tensor-parallel-size",
        "2",
        "--max-model-len",
        "262144",
        "--gpu-memory-utilization",
        "0.90",
        "--max-num-seqs",
        "256",
    ],
    "requests": {"cpu": "16", "memory": "96Gi", "nvidia.com/gpu": "2"},
    "limits": {"nvidia.com/gpu": "2"},
}

LABELS = {
    "dynamo-component": "VllmDecodeWorker",
    "dynamo-graph-deployment-name": "qwen3-8-27b-agg",
    "dynamo-namespace": "dynamo-qwen3-8-27b-agg",
}


def test_a_dynamo_vllm_worker_reads_as_a_complete_serving_block() -> None:
    draft = derive(
        spec=QWEN,
        labels=LABELS,
        name="qwen3-8-27b-agg-vllmdecodeworker-b16ef7d1",
        devices_per_replica=2,
        device_class="rtx-pro-6000-blackwell",
    )

    assert draft.confident
    assert draft.notes == []
    assert draft.engine == "vllm"
    assert draft.engine_runtime_version == "1.3.1"
    assert draft.artifact_ref == "hf:Qwen/Qwen3.8-27B"
    assert draft.artifact_name == "Qwen/Qwen3.8-27B"
    assert draft.name == "qwen3.8-27b"
    assert draft.context_length == 262144
    assert draft.tensor_parallel == 2
    assert draft.device_count == 2
    assert draft.max_batch_tokens is None, "--max-num-seqs is not --max-num-batched-tokens"
    assert draft.imported_args["--max-num-seqs"] == "256"
    assert draft.kv_cache_fraction == 0.90
    assert draft.cpu_cores == 16
    assert draft.memory_mib == 96 * 1024
    assert draft.device_class == "rtx-pro-6000-blackwell"


def test_quantisation_is_read_from_the_model_name_when_the_args_are_silent() -> None:
    spec = dict(QWEN)
    spec["args"] = [
        "--model",
        "prism-ml/Ternary-Bonsai-27B-AWQ-4bit",
        "--served-model-name",
        "ternary-bonsai-27b",
        "--tensor-parallel-size",
        "1",
        "--max-model-len",
        "262144",
    ]
    draft = derive(spec=spec, labels={}, name="w", devices_per_replica=1)
    assert draft.quantization == "awq"


def test_an_explicit_quantisation_argument_wins_over_the_name() -> None:
    spec = dict(QWEN)
    spec["args"] = QWEN["args"] + ["--quantization", "fp8"]
    draft = derive(spec=spec, labels={}, name="w", devices_per_replica=2)
    assert draft.quantization == "fp8"


def test_every_argument_read_survives_as_an_imported_one() -> None:
    spec = dict(QWEN)
    spec["args"] = QWEN["args"] + ["--enforce-eager", "--swap-space", "8"]
    draft = derive(spec=spec, labels={}, name="w", devices_per_replica=2)
    assert draft.imported_args["--enforce-eager"] == ""
    assert draft.imported_args["--swap-space"] == "8"
    assert draft.imported_args["--model"] == "Qwen/Qwen3.8-27B", (
        "arguments with a column are repeated here too — the record is of what was read, "
        "and a partial one cannot be checked against the cluster"
    )


def test_reading_a_workload_declares_nothing() -> None:
    """A draft proposes no engine_args at all.

    Reading a flag off a running pod records it; asserting it is a separate act by
    a person. Only engine_args renders onto a command line (ADR 0009).
    """
    spec = dict(QWEN)
    spec["args"] = QWEN["args"] + ["--enforce-eager"]
    draft = derive(spec=spec, labels={}, name="w", devices_per_replica=2)
    assert draft.engine_args == {}
    assert draft.imported_args


def test_an_unreadable_engine_is_reported_rather_than_guessed() -> None:
    draft = derive(
        spec={"image": "ghcr.io/acme/something", "args": []},
        labels={},
        name="mystery",
        devices_per_replica=1,
    )
    assert not draft.confident
    assert draft.engine is None
    assert any("which engine" in n for n in draft.notes)


def test_parallelism_disagreeing_with_the_pod_is_called_out() -> None:
    spec = dict(QWEN)
    spec["args"] = ["--model", "m", "--max-model-len", "1024", "--tensor-parallel-size", "4"]
    draft = derive(spec=spec, labels={}, name="w", devices_per_replica=2)
    assert any("holds 2 devices" in n for n in draft.notes)
    assert draft.device_count == 4, "the larger of the two, never silently the smaller"


def test_a_name_falls_back_to_the_graph_deployment_then_the_workload() -> None:
    spec = dict(QWEN)
    spec["args"] = ["--model", "m", "--max-model-len", "8"]
    assert derive(spec=spec, labels=LABELS, name="w", devices_per_replica=1).name == "qwen3-8-27b-agg"
    assert derive(spec=spec, labels={}, name="w", devices_per_replica=1).name == "w"


def test_equals_form_arguments_parse() -> None:
    assert parse_args(["--model=a/b", "--tensor-parallel-size=2"]) == {
        "model": "a/b",
        "tensor-parallel-size": "2",
    }


def test_millicore_requests_round_up_to_a_whole_core() -> None:
    draft = derive(
        spec={"image": QWEN["image"], "args": QWEN["args"], "requests": {"cpu": "500m"}},
        labels={},
        name="w",
        devices_per_replica=2,
    )
    assert draft.cpu_cores == 1


def test_engine_is_read_from_the_runtime_image_convention() -> None:
    assert engine_runtime_from_image("nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1") == ("vllm", "1.3.1")
    assert engine_runtime_from_image("nvcr.io/nvidia/ai-dynamo/sglang-runtime:0.4.2") == ("sglang", "0.4.2")
    assert engine_runtime_from_image(None) == (None, None)


def test_weights_read_as_an_artifact_named_by_reference() -> None:
    draft = derive(spec=QWEN, labels=LABELS, name="w", devices_per_replica=2)
    art = draft.artifact
    assert art.reference == "hf:Qwen/Qwen3.8-27B"
    assert art.digest == art.reference
    assert art.source_scheme == "hf"
    # What the operator turns back into `--model` when it renders the serving block.
    assert art.source_uri == "hf://Qwen/Qwen3.8-27B"
    assert art.verified is False
    assert any("not a hash of the bytes" in note for note in art.notes)


def test_a_revision_sharpens_the_reference_and_its_absence_is_said() -> None:
    spec = {**QWEN, "args": [*QWEN["args"], "--revision", "a1b2c3d"]}
    draft = derive(spec=spec, labels={}, name="w", devices_per_replica=2)
    assert draft.artifact.reference == "hf:Qwen/Qwen3.8-27B@a1b2c3d"
    assert draft.artifact.revision == "a1b2c3d"

    bare = derive(spec=QWEN, labels={}, name="w", devices_per_replica=2)
    assert any("names a repository rather than a commit" in note for note in bare.artifact.notes)


def test_parameter_count_is_read_from_the_name_and_flagged_as_such() -> None:
    draft = derive(spec=QWEN, labels={}, name="w", devices_per_replica=2)
    assert draft.artifact.parameter_count == 27_000_000_000
    assert any("from the model's name" in note for note in draft.artifact.notes)

    spec = {**QWEN, "args": ["--model", "mistralai/Mixtral-8x7B-Instruct-v0.1"]}
    moe = derive(spec=spec, labels={}, name="w", devices_per_replica=2)
    assert moe.artifact.parameter_count == 56_000_000_000


def test_local_weights_are_a_path_nothing_can_fetch() -> None:
    spec = {**QWEN, "args": ["--model", "/models/qwen3-27b"]}
    draft = derive(spec=spec, labels={}, name="w", devices_per_replica=2)
    assert draft.artifact.reference == "path:/models/qwen3-27b"
    assert draft.artifact.source_scheme == "file"
    assert draft.artifact.name == "qwen3-27b"
    assert any("path inside the cluster" in note for note in draft.artifact.notes)


def test_a_workload_with_no_model_argument_identifies_no_weights() -> None:
    spec = {"image": QWEN["image"], "args": ["--max-model-len", "8192"]}
    draft = derive(spec=spec, labels={}, name="w", devices_per_replica=1)
    assert draft.artifact.reference is None
    assert draft.artifact.digest is None
    assert draft.artifact.notes == ["no --model argument, so there are no weights to identify"]
