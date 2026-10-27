"""Reading a sighting's pod template as a workload's serving block.

Every cluster we operate runs Dynamo over vLLM, and this leans on that hard. It is not a
general parser and does not pretend to be one: it knows what `python3 -m dynamo.vllm`
takes, what NVIDIA's runtime images are called, and what Dynamo writes into its labels.
A cluster serving something else produces a draft with fewer fields filled and a note
saying so, which is the honest outcome — a wrong guess is worse than a blank.

It lives in the plane rather than the operator on purpose. Rule 2 says the operator is the
only thing that knows Kubernetes; it does not say the operator is the thing that knows
vLLM. Keeping engine knowledge here means a better reading ships as a plane deploy instead
of a roll across every cluster in the fleet.

Nothing here writes. It proposes, and a person confirms.
"""

import re

from app.core.schemas import Struct


# Dynamo's own labels, which say more about a workload than most operators say anywhere.
OMNI_MODULE = "dynamo.vllm.omni"
"""The module an omni worker runs, which is how a sighting is read as one: its image is the
same `vllm-runtime` a chat worker runs, so only the command line tells them apart."""

LABEL_GRAPH = "dynamo-graph-deployment-name"
LABEL_COMPONENT = "dynamo-component"
LABEL_COMPONENT_TYPE = "dynamo-component-type"
LABEL_NAMESPACE = "dynamo-namespace"

# `nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1` -> engine `vllm`, runtime version `1.3.1`.
_RUNTIME_IMAGE = re.compile(r"/(?P<engine>[a-z0-9]+)-runtime:(?P<version>[^@\s]+)")
_ANY_IMAGE = re.compile(r"/(?P<engine>[a-z0-9]+)[^/:]*:(?P<version>[^@\s]+)")


class ArtifactDraft(Struct):
    """The weights a sighting is serving, as far as they can be identified.

    A cluster reports what it was told to load, never what it loaded. `--model
    Qwen/Qwen3-32B` is a name someone typed, not a hash of any bytes, so `digest`
    here is that reference and `verified` is false: the row is a real artifact and
    workloads may point at it, and nothing should mistake it for content-addressed
    until something resolves the weights.

    `existing_id` is filled by the service, not by this module. Derivation is a
    pure reading of one pod template; whether the catalogue already holds these
    weights is a question about the database.
    """

    reference: str | None
    digest: str | None
    name: str | None
    kind: str
    format: str | None
    revision: str | None
    parameter_count: int | None
    source_scheme: str | None
    source_uri: str | None
    verified: bool
    metadata: dict
    existing_id: str | None
    notes: list[str]


class ServingDraft(Struct):
    """A proposed serving block, and an account of how much of it was actually read.

    It drafts the block a workload is created with, not an object of its own. There
    is no recipe to save this into any more: the fields below land on the workload
    the promotion mints, and two adoptions that read the same command line arrive at
    the same `serving_digest` without either of them naming the other.

    `confident` is false when the engine could not be identified, because every
    field below it is then a guess dressed as a reading. `notes` names what could
    not be determined so the person confirming knows where to look rather than
    having to diff the draft against the cluster themselves.

    `imported_args` is the argv as discovery reported it, in flag/value pairs, including
    arguments that also populated a field above. Flags whose values discovery withheld are
    named in `notes` and not listed here. `engine_args` is empty in a draft and always
    will be: reading a flag off a pod records it, and asserting it is a separate
    act by a person (ADR 0009). The duplication is deliberate — the draft has to
    be checkable against the cluster without re-reading the pod.
    """

    artifact_ref: str | None
    artifact_name: str | None
    name: str
    engine: str | None
    engine_runtime_version: str | None
    engine_runtime_image: str | None
    engine_args: dict
    imported_args: dict
    context_length: int | None
    tensor_parallel: int
    pipeline_parallel: int
    expert_parallel: int
    max_batch_tokens: int | None
    kv_cache_fraction: float | None
    quantization: str | None
    dtype: str | None
    device_count: int
    device_class: str | None
    cpu_cores: int | None
    memory_mib: int | None
    confident: bool
    notes: list[str]
    artifact: ArtifactDraft
    output_modality: str | None = None
    """`--output-modalities` off an omni worker, and None on every other engine."""


REDACTED = "<redacted>"
"""What the operator sends in place of a flag value it does not report (`observe.RedactedValue`)."""


def parse_args(args: list[str]) -> dict[str, str]:
    """Folds a container's argv into flag/value pairs.

    A bare `--flag` with no value maps to the empty string rather than being
    dropped: `--enforce-eager` is a setting, and losing it would make the draft
    quietly disagree with what is running.
    """
    out: dict[str, str] = {}
    i = 0
    while i < len(args):
        token = args[i]
        if not token.startswith("--"):
            i += 1
            continue
        if "=" in token:
            flag, _, value = token.partition("=")
            out[flag[2:]] = value
            i += 1
            continue
        key = token[2:]
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            out[key] = args[i + 1]
            i += 2
            continue
        out[key] = ""
        i += 1
    return out


def engine_runtime_from_image(image: str | None) -> tuple[str | None, str | None]:
    """Reads the engine and the runtime version out of a runtime image address.

    `vllm-runtime:1.3.1` is two facts in one string: which engine is compiled in,
    and which Dynamo release compiled it. The tag is the second and never the
    first — vLLM's own version is not in this address and cannot be read out of it.

    NVIDIA names these `<engine>-runtime`, which is a stronger signal than the
    repository path and is tried first. Anything else falls back to the last path
    segment, which is right often enough to be worth offering and always visible
    to whoever confirms the draft.
    """
    if not image:
        return None, None
    if m := _RUNTIME_IMAGE.search(image):
        return m.group("engine"), m.group("version")
    if m := _ANY_IMAGE.search(image):
        return m.group("engine"), m.group("version")
    return None, None


def _int(values: dict[str, str], key: str) -> int | None:
    try:
        return int(values[key])
    except (KeyError, ValueError):
        return None


def _float(values: dict[str, str], key: str) -> float | None:
    try:
        return float(values[key])
    except (KeyError, ValueError):
        return None


def _cpu_cores(requests: dict[str, str]) -> int | None:
    """Kubernetes cpu as whole cores, rounding a millicore request up.

    A workload asking for `500m` is asking for less than a core and gets one here:
    a workload's `cpu_cores` is a floor for placement, and rounding down would
    schedule a replica onto less cpu than the workload already runs with.
    """
    raw = requests.get("cpu")
    if not raw:
        return None
    try:
        if raw.endswith("m"):
            millis = int(raw[:-1])
            return max(1, -(-millis // 1000))
        return max(1, int(float(raw)))
    except ValueError:
        return None


_MEMORY_UNITS = {
    "Ki": 1 / 1024,
    "Mi": 1,
    "Gi": 1024,
    "Ti": 1024 * 1024,
    "K": 1000 / (1024 * 1024),
    "M": 1000 * 1000 / (1024 * 1024),
    "G": 1000 * 1000 * 1000 / (1024 * 1024),
}


def _memory_mib(requests: dict[str, str]) -> int | None:
    raw = requests.get("memory")
    if not raw:
        return None
    for suffix, factor in _MEMORY_UNITS.items():
        if raw.endswith(suffix):
            try:
                return int(float(raw[: -len(suffix)]) * factor)
            except ValueError:
                return None
    try:
        return int(int(raw) / (1024 * 1024))
    except ValueError:
        return None


def _quantization(model_ref: str | None, values: dict[str, str]) -> str | None:
    """What the weights are quantised to, preferring what the engine was told.

    The model name is consulted only when the args are silent, because vLLM reads
    quantisation out of the checkpoint's own config and a repository called
    `...-AWQ-4bit` is a naming convention rather than a declaration. It is a hint
    offered for confirmation, never a fact asserted.
    """
    if explicit := values.get("quantization"):
        return explicit
    if not model_ref:
        return None
    lowered = model_ref.lower()
    for known in ("awq", "gptq", "fp8", "int8", "int4", "nvfp4"):
        if known in lowered:
            return known
    return None


# `Qwen3-32B`, `Llama-3.1-70B-Instruct`, `Mixtral-8x7B` — the parameter count is in the name often
# enough to be worth offering, and is a naming convention rather than a fact, so it is carried as a
# hint a person confirms.
_PARAMS = re.compile(r"(?:^|[-_./])(?P<count>\d+(?:\.\d+)?)\s*[bB](?:$|[-_./])")
_MOE_PARAMS = re.compile(r"(?:^|[-_./])(?P<experts>\d+)x(?P<count>\d+(?:\.\d+)?)\s*[bB](?:$|[-_./])")

# What `--load-format` says the bytes are, where it says anything at all.
_LOAD_FORMATS = {
    "safetensors": "safetensors",
    "pt": "pytorch",
    "npcache": "pytorch",
    "gguf": "gguf",
    "bitsandbytes": "bitsandbytes",
    "mistral": "safetensors",
}


def _parameter_count(name: str | None) -> int | None:
    """Reads a parameter count out of a model name, or gives up.

    `8x7B` is the total, not the active count: a Mixtral is 56B of weights that
    must be resident, and residency is what an artifact's size is about.
    """
    if not name:
        return None
    if m := _MOE_PARAMS.search(name):
        return int(float(m.group("count")) * int(m.group("experts")) * 1_000_000_000)
    if m := _PARAMS.search(name):
        return int(float(m.group("count")) * 1_000_000_000)
    return None


def _artifact_format(model_ref: str | None, values: dict[str, str]) -> str | None:
    """What the weights are stored as, said only when the engine was told.

    Absent otherwise: safetensors is the safe guess for almost every repository on
    the Hub, and a guess written into a field that reads as a statement of fact is
    exactly what this module exists not to do.
    """
    if fmt := _LOAD_FORMATS.get((values.get("load-format") or "").lower()):
        return fmt
    if model_ref and model_ref.lower().endswith(".gguf"):
        return "gguf"
    return None


def derive_artifact(model_ref: str | None, values: dict[str, str], quantization: str | None) -> ArtifactDraft:
    """Reads the weights a workload was pointed at.

    A repository id becomes an `hf` source, because that is what the operator turns
    back into `--model` when it renders a workload — adopting weights by reference
    and serving them by reference are the same string, and an artifact that cannot
    be rendered back is not worth writing.

    A local path is kept as a `file` source and named as one. It is honest and it
    is nearly useless: a path is meaningful only on the cluster that holds it, so
    the draft says so rather than implying the weights are fetchable.
    """
    notes: list[str] = []
    metadata: dict = {"source": "discovered"}

    if not model_ref:
        return ArtifactDraft(
            reference=None,
            digest=None,
            name=None,
            kind="weights",
            format=None,
            revision=None,
            parameter_count=None,
            source_scheme=None,
            source_uri=None,
            verified=False,
            metadata=metadata,
            existing_id=None,
            notes=["no --model argument, so there are no weights to identify"],
        )

    revision = values.get("revision") or values.get("code-revision") or None
    local = model_ref.startswith(("/", "./"))

    if local:
        scheme, uri = "file", model_ref
        notes.append("the weights are a path inside the cluster, so nothing here can fetch them")
    else:
        scheme, uri = "hf", f"hf://{model_ref}"

    reference = f"hf:{model_ref}" if not local else f"path:{model_ref}"
    if revision:
        reference = f"{reference}@{revision}"
    else:
        notes.append("no --revision, so this names a repository rather than a commit of one")

    if tokenizer := values.get("tokenizer"):
        metadata["tokenizer"] = tokenizer
    if served := values.get("served-model-name"):
        metadata["served_model_name"] = served
    if quantization:
        metadata["quantization"] = quantization
    if dtype := values.get("dtype"):
        metadata["dtype"] = dtype
    if download_dir := values.get("download-dir"):
        metadata["download_dir"] = download_dir

    parameter_count = _parameter_count(model_ref)
    if parameter_count is not None:
        notes.append("the parameter count was read from the model's name, not from its config")

    notes.append("the digest is this reference, not a hash of the bytes — nothing has resolved them")

    return ArtifactDraft(
        reference=reference,
        digest=reference,
        name=model_ref.rstrip("/").split("/")[-1] if local else model_ref,
        kind="weights",
        format=_artifact_format(model_ref, values),
        revision=revision,
        parameter_count=parameter_count,
        source_scheme=scheme,
        source_uri=uri,
        verified=False,
        metadata=metadata,
        existing_id=None,
        notes=notes,
    )


def derive(
    *,
    spec: dict,
    labels: dict[str, str],
    name: str,
    devices_per_replica: int,
    device_class: str | None = None,
) -> ServingDraft:
    """Proposes a serving block from what one cluster reported about one workload."""
    notes: list[str] = []
    args = list(spec.get("args") or [])
    values = parse_args(args)
    image = spec.get("image")
    engine, engine_runtime_version = engine_runtime_from_image(image)
    omni = OMNI_MODULE in list(spec.get("command") or []) + args
    if omni:
        engine = "omni"

    if engine is None:
        notes.append("could not tell which engine this runs; every field below is unread")
    elif engine not in ("vllm", "omni"):
        notes.append(f"read as {engine!r}, but only vLLM arguments are understood")

    model_ref = values.get("model") or None
    if model_ref is None:
        notes.append("no --model argument, so the weights could not be identified")

    context_length = _int(values, "max-model-len")
    output_modality = values.get("output-modalities") or None
    if omni:
        if output_modality not in ("image", "video"):
            notes.append("an omni worker with no --output-modalities image or video; set it by hand")
        if "stage-configs-path" in values:
            notes.append(
                "the stage configuration is a file inside the pod and cannot be read from here; "
                "declare serving.diffusion to keep it across the next render"
            )
    elif context_length is None:
        notes.append("no --max-model-len, so context length needs setting by hand")

    tensor_parallel = _int(values, "tensor-parallel-size") or 1
    pipeline_parallel = _int(values, "pipeline-parallel-size") or 1
    expert_parallel = _int(values, "expert-parallel-size") or 1

    device_count = max(devices_per_replica, tensor_parallel * pipeline_parallel)
    if devices_per_replica and device_count != devices_per_replica:
        notes.append(
            f"the pod holds {devices_per_replica} devices but the parallelism implies "
            f"{tensor_parallel * pipeline_parallel}"
        )

    requests = dict(spec.get("requests") or {})

    # Every argument the workload was running with as discovery reported it, including the ones
    # that also landed in a first-class field above. This is a record of what was read and not a
    # proposal to render it (ADR 0009). It is not the full command line: the operator reports the
    # values of the serving flags only and sends REDACTED for every other flag that took one, so
    # those flags are named in a note and left out here, where the placeholder would read as a value.
    withheld = sorted(f"--{k}" for k, v in values.items() if v == REDACTED)
    if withheld:
        notes.append(f"discovery withheld the values of {', '.join(withheld)}; set them by hand if they matter")
    imported_args = {f"--{k}": v for k, v in values.items() if v != REDACTED}

    # Nothing is declared by reading. A draft proposes no engine_args at all: asserting a flag
    # is somebody's decision, and the whole point of the split is that it stays visible as one.
    engine_args: dict[str, str] = {}

    return ServingDraft(
        artifact_ref=f"hf:{model_ref}" if model_ref else None,
        artifact_name=model_ref,
        name=values.get("served-model-name") or labels.get(LABEL_GRAPH) or name,
        engine=engine,
        engine_runtime_version=engine_runtime_version,
        engine_runtime_image=image,
        engine_args=engine_args,
        imported_args=imported_args,
        context_length=context_length,
        tensor_parallel=tensor_parallel,
        pipeline_parallel=pipeline_parallel,
        expert_parallel=expert_parallel,
        max_batch_tokens=_int(values, "max-num-batched-tokens"),
        kv_cache_fraction=_float(values, "gpu-memory-utilization"),
        quantization=_quantization(model_ref, values),
        dtype=values.get("dtype"),
        device_count=device_count,
        device_class=device_class,
        cpu_cores=_cpu_cores(requests),
        memory_mib=_memory_mib(requests),
        confident=model_ref is not None
        and (
            (engine == "vllm" and context_length is not None)
            or (engine == "omni" and output_modality in ("image", "video"))
        ),
        notes=notes,
        artifact=derive_artifact(model_ref, values, _quantization(model_ref, values)),
        output_modality=output_modality if omni else None,
    )
