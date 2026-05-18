"""Reading a HuggingFace slug as a proposed artifact.

The plane derives the digest rather than letting the client assert one. A client
that types a digest is making a claim nobody checked, and two clients registering
the same weights would land on two identities; deriving it from the Hub's per-file
LFS oids means the same repo at the same commit always resolves to the same
artifact, without a byte of weights crossing the control plane.

Nothing here writes. It proposes, and the caller confirms — either by looking at
the draft or by posting `source` and letting `create` apply it.
"""

import math

from app.core import devices
from app.core.vram import DEFAULT_QUANTIZATION
from app.core.vram import is_moe
from app.core.vram import estimate_vram
from app.core.vram import extract_shape
from app.core.vram import shape_metadata
from app.core.vram import effective_config
from app.core.vram import native_quant_method
from app.core.vram import derive_active_params
from app.core.schemas import Struct
from app.integrations import vllm_recipes
from app.integrations.huggingface import HuggingFaceModel

from .schemas import INFO
from .schemas import WARN
from .schemas import ResolveNote
from .schemas import ArtifactLocationRequest


SCHEME = "hf"

_FALLBACK_DEVICE_MEMORY_GB = 80
"""Assumed largest accelerator when the device table cannot be read.

The table is the authority and `devices.table().largest()` is the real answer; this
only keeps a suggestion arithmetically possible on a broken install, where the
alternative is dividing a memory requirement by nothing.
"""

_MIB_PER_GB = 1024

FALLBACK_CONTEXT_LENGTH = 8192
"""Context assumed when a config declares none, only so the sizing has a number."""


class RecipeDefaults(Struct):
    """Serving parameters implied by the weights, offered and never applied.

    A recipe is a declared object a person owns; sizing it from `config.json` is a
    starting point, not a decision. The plane surfaces these so the console can
    pre-fill a form, and a recipe only ever gets them because someone submitted them.
    """

    device_count: int
    device_memory_mib_min: int
    context_length: int | None
    quantization: str | None
    dtype: str | None


class UpstreamDeployment(Struct):
    """What the corpus's own rules render for one node shape.

    A large model has no single tested parallelism — it has one per node it can
    run on. Kimi K3 is TP 8 on an H200 and TP 8 on a GB300 tray of four, needs one
    node by the corpus's declared GPU floor and four on an H100, and cannot use
    `multi_node_dep` on either. Flattening that to one number loses every part of
    it that a placement decision turns on.

    `nodes_declared` is the corpus's floor — `strategy_min_gpus`, resolved their
    way. `nodes_by_memory` is our own division of the published memory floor by
    what the node holds, and the two answer different questions: theirs is what
    the layout needs to work at all, ours is what the weights need to fit. The
    larger governs, and they are kept apart so a reader can see which one bound.
    """

    hardware: str
    display_name: str
    gpu_count: int
    tensor_parallel: int
    nodes_declared: int
    nodes_by_memory: int
    strategies: list[str]


class UpstreamRecipe(Struct):
    """Serving parameters somebody upstream has run, attributed to them.

    Kept apart from `RecipeDefaults` on purpose. That struct is the plane's own
    arithmetic over `config.json`, and this is a third party's measurement of a
    different question — not "what do these weights need" but "what did this
    configuration do when we served it". Merging the two would produce a draft
    where no field can say where its number came from, and the numbers do not
    always agree.

    Every field is a claim, and `source_uri` is a permalink at a pinned commit so
    the claim can be read back. Nothing here is a fact about our hardware:
    `verified_hardware` names the node shapes vLLM ran on, which is evidence that
    the configuration works and no evidence at all that we own such a node.

    `args` is the whole tested command line — base flags, the variant's flags, and
    the flags of every feature that is on by default. The parsers are lifted out
    of it because the recipe has typed fields for them; the list stays whole
    because the flags we have no field for are exactly the ones a person needs to
    see before they copy the configuration.
    """

    source: str
    source_uri: str
    model_id: str
    variant: str
    precision: str | None
    engine: str
    engine_version_min: str | None
    nightly_required: bool
    tensor_parallel: int | None
    default_strategy: str | None
    deployments: list[UpstreamDeployment]
    replica_memory_mib_min: int | None
    context_length: int | None
    max_model_len: int | None
    kv_cache_dtype: str | None
    tool_call_parser: str | None
    reasoning_parser: str | None
    args: list[str]
    env: dict
    features_on: list[str]
    features_opt_in: list[str]
    strategies: list[str]
    requires_single_topology_domain: bool
    verified_hardware: list[str]


class ResolvedArtifact(Struct):
    """A source manifest read as an artifact, ready to be registered.

    The field names overlap `discovered.derive.ArtifactDraft` deliberately: both
    answer "what artifact is this", and two drafts naming the same fact differently
    is how a console ends up with two ways to register one set of weights.

    Unlike that draft, `digest` here is content-derived whenever the Hub exposes an
    oid for every weight file, which is what makes `verified` true. When it does
    not, the digest names the commit instead and `notes` says so.
    """

    reference: str
    digest: str
    name: str
    kind: str
    format: str | None
    revision: str | None
    size_bytes: int
    licence: str | None
    parameter_count: int | None
    source_scheme: str
    source_uri: str
    verified: bool
    metadata: dict
    locations: list[ArtifactLocationRequest]
    recipe: RecipeDefaults
    upstream: UpstreamRecipe | None
    existing_id: str | None
    notes: list[ResolveNote]


def normalise_source(source: str) -> str:
    """Folds `owner/name`, `hf:owner/name` and `hf://owner/name` onto one form.

    Callers arrive from a form field, a CLI flag and a `--model` argument copied
    out of a running workload, and all three spellings are in the wild. Storing
    whichever one was typed would make `source_uri` a poor key for "same weights".

    Returns the canonical `hf://owner/name`.
    """
    value = source.strip()
    for prefix in ("hf://", "hf:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    return f"hf://{value.strip('/')}"


def slug_of(source: str) -> str:
    """The `owner/name` inside a normalised source, or the empty string when malformed."""
    slug = normalise_source(source)[len("hf://") :]
    parts = [part for part in slug.split("/") if part]
    if len(parts) != 2:
        return ""
    return "/".join(parts)


def source_of(metadata: dict, uris: tuple[str, ...] = ()) -> str | None:
    """The source a registered artifact can be re-read from, or None.

    `metadata.huggingface_id` first, because that is what `resolved` wrote and it
    names the repo without a revision attached. The locations are the fallback for
    artifacts registered before that key existed or adopted by reference from a
    running workload — their uris carry `@revision`, which is dropped: a refresh
    asks the repo what it says now, and pinning the commit it said it at would make
    the answer the one already stored.

    Returns the canonical `hf://owner/name`, or None for an artifact whose bytes
    came from somewhere the plane cannot go back and ask.
    """
    slug = metadata.get("huggingface_id")
    if isinstance(slug, str) and slug_of(slug):
        return normalise_source(slug)
    for uri in uris:
        candidate = uri.split("@")[0]
        if candidate.lower().startswith(("hf:", "hf://")) and slug_of(candidate):
            return normalise_source(candidate)
    return None


def _dtype_of(config: dict) -> str | None:
    eff = effective_config(config)
    value = eff.get("torch_dtype") or eff.get("dtype")
    return value if isinstance(value, str) else None


def _architecture_of(config: dict) -> str | None:
    eff = effective_config(config)
    architectures = eff.get("architectures")
    if isinstance(architectures, list) and architectures and isinstance(architectures[0], str):
        return architectures[0]
    model_type = eff.get("model_type")
    return model_type if isinstance(model_type, str) else None


def _shardable(count: int) -> int:
    """Rounds a device count up to a power of two, floored at one.

    Tensor parallelism splits attention heads across devices and every engine we
    run wants that split to come out even, so a suggestion of five devices is
    arithmetically correct and cannot be served. Powers of two are what nodes are
    built in and what the engines accept.
    """
    shards = 1
    while shards < count:
        shards *= 2
    return shards


def suggest_recipe(model: HuggingFaceModel, notes: list[ResolveNote]) -> RecipeDefaults:
    """Sizes a first recipe from the weights, then says what hardware that needs.

    The direction matters. The serving parameters are the repo's own: its native
    quantization, its dtype, and the full context window it declares. Those are
    read and never trimmed — an artifact knows nothing about what we operate, so
    it cannot be the thing that decides a 131k model should be served at 8k. The
    device shape is the consequence: how much memory that configuration needs, and
    across how many accelerators it has to spread to get it.

    Quantization is resolved from the repo rather than defaulted: a repo shipping
    mxfp4 sized as fp16 overcounts weights fourfold, which turns a suggestion into
    a placement that never schedules.

    The suggestion and the sizing want different answers when a repo declares no
    quantization at all. Sizing needs a bytes-per-param bucket and fp16 is the right
    one for unquantized weights; the suggestion needs to stay empty, because it is
    offered as the recipe's `--quantization` and fp16 is a dtype, not a quantization
    method any engine accepts. Defaulting the two together renders every unquantized
    repo into a recipe the engine rejects before it binds a port.
    """
    quantization = native_quant_method(model.config, model.slug)
    sizing_quantization = quantization or DEFAULT_QUANTIZATION
    dtype = _dtype_of(model.config)
    shape = extract_shape(model.config)

    if shape is None or not model.parameter_count:
        notes.append(
            ResolveNote(
                level=INFO,
                message="the config carries no attention shape, so no device sizing is suggested",
            )
        )
        return RecipeDefaults(
            device_count=1,
            device_memory_mib_min=0,
            context_length=None,
            quantization=quantization,
            dtype=dtype,
        )

    context_length = shape.context_length or FALLBACK_CONTEXT_LENGTH
    required_gb = estimate_vram(
        total_params=model.parameter_count,
        shape=shape,
        quantization=sizing_quantization,
        context_length=context_length,
        concurrency=1,
    ).required_gb

    largest = devices.table().largest()
    ceiling_gb = largest.memory_mib / _MIB_PER_GB if largest else _FALLBACK_DEVICE_MEMORY_GB
    device_count = _shardable(math.ceil(required_gb / ceiling_gb))
    per_device_gb = math.ceil(required_gb / device_count)
    if device_count > 1:
        notes.append(
            ResolveNote(
                level=INFO,
                message=(
                    f"serving the declared context of {context_length:,} tokens needs about "
                    f"{math.ceil(required_gb)} GB, so the suggestion spreads it across {device_count} devices"
                ),
            )
        )

    return RecipeDefaults(
        device_count=device_count,
        device_memory_mib_min=per_device_gb * _MIB_PER_GB,
        context_length=context_length,
        quantization=quantization,
        dtype=dtype,
    )


_UPSTREAM_SOURCE = "vllm-recipes"

_UPSTREAM_ENGINE = "vllm"

_SINGLE_NODE_TP = "single_node_tp"

_DISAGREEMENT_RATIO = 2.0
"""How far the two sizings may drift before the difference is worth a warning.

Below this they are two honest answers to slightly different questions — ours
sizes the declared context at concurrency one, theirs is a floor measured under
whatever load they ran. Above it one of them is wrong about the weights, and
that is a thing to look at before the recipe is submitted.
"""


def upstream_recipe(
    model: HuggingFaceModel,
    defaults: RecipeDefaults,
    notes: list[ResolveNote],
) -> UpstreamRecipe | None:
    """The vLLM recipe for these weights, or None when the corpus has none.

    A miss is the ordinary case: the corpus covers a few hundred models and says
    nothing about the rest. It produces no note, because "nobody upstream has
    published a recipe for this" is not a remark about the weights.

    The lookup takes the slug as typed. A slug that is a requantized variant —
    `RedHatAI/...-FP8-dynamic` — resolves to the family recipe *and* to the
    variant inside it, so the precision is read rather than guessed.
    """
    match = vllm_recipes.lookup(model.slug)
    if match is None:
        return None

    strategies = vllm_recipes.corpus().strategies_for(match.recipe)
    replica_memory_mib_min = match.replica_memory_mib_min()

    _note_disagreement(defaults, replica_memory_mib_min, notes)

    return UpstreamRecipe(
        source=_UPSTREAM_SOURCE,
        source_uri=match.source_uri,
        model_id=match.recipe.slug,
        variant=match.variant.key,
        precision=match.variant.precision or None,
        engine=_UPSTREAM_ENGINE,
        engine_version_min=(match.variant.min_vllm_version or match.recipe.min_vllm_version) or None,
        nightly_required=match.recipe.nightly_required,
        tensor_parallel=match.declared_tensor_parallel(),
        default_strategy=match.recipe.default_strategy or None,
        deployments=_deployments(match, replica_memory_mib_min),
        replica_memory_mib_min=replica_memory_mib_min,
        context_length=match.recipe.context_length,
        max_model_len=_as_int(match.flag("--max-model-len")),
        kv_cache_dtype=match.flag("--kv-cache-dtype"),
        tool_call_parser=match.flag("--tool-call-parser"),
        reasoning_parser=match.flag("--reasoning-parser"),
        args=list(match.args()),
        env=dict(match.variant.extra_env),
        features_on=list(match.recipe.default_features),
        features_opt_in=list(match.recipe.opt_in_features),
        strategies=[strategy.name for strategy in strategies],
        requires_single_topology_domain=bool(strategies) and all(not s.multi_node for s in strategies),
        verified_hardware=list(match.recipe.verified_hardware),
    )


def _deployments(
    match: vllm_recipes.RecipeMatch,
    replica_memory_mib_min: int | None,
) -> list[UpstreamDeployment]:
    """The corpus's rules applied to each node shape it claims a run on.

    Only `verified` hardware, because an unverified profile would produce numbers
    with the same shape and no evidence behind them, and the two would be
    indistinguishable in a table.
    """
    corpus = vllm_recipes.corpus()
    out = []
    for name in match.recipe.verified_hardware:
        profile = corpus.hardware_profiles.get(name)
        if profile is None:
            continue
        node_memory_mib = profile.vram_gb * _MIB_PER_GB
        by_memory = 1
        if replica_memory_mib_min and node_memory_mib:
            by_memory = max(1, math.ceil(replica_memory_mib_min / node_memory_mib))
        out.append(
            UpstreamDeployment(
                hardware=name,
                display_name=profile.display_name or name,
                gpu_count=profile.gpu_count,
                tensor_parallel=match.tensor_parallel_for(profile),
                nodes_declared=match.nodes_for(match.recipe.default_strategy or _SINGLE_NODE_TP, profile),
                nodes_by_memory=by_memory,
                strategies=[s.name for s in corpus.strategies_for(match.recipe) if match.supports(s.name, profile)],
            )
        )
    return out


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _note_disagreement(
    defaults: RecipeDefaults,
    replica_memory_mib_min: int | None,
    notes: list[ResolveNote],
) -> None:
    """Says so when our arithmetic and the published measurement are far apart.

    Both numbers are memory across the whole replica, which is the only unit in
    which the two compare at all: eight devices at 40 GB and four at 80 GB are one
    sizing, and a per-device comparison would call them different. The published
    figure is already a replica total and must not be multiplied by the tested
    parallelism — doing that counts a 1.6 TB model eight times over.
    """
    if not replica_memory_mib_min:
        return
    ours = defaults.device_count * defaults.device_memory_mib_min
    if not ours:
        return
    ratio = max(ours, replica_memory_mib_min) / min(ours, replica_memory_mib_min)
    if ratio < _DISAGREEMENT_RATIO:
        return
    notes.append(
        ResolveNote(
            level=WARN,
            message=(
                f"the sizing derived here needs about {ours // _MIB_PER_GB} GB across the replica, "
                f"while the published vLLM recipe was served in {replica_memory_mib_min // _MIB_PER_GB} GB — "
                "the two disagree by more than a factor of two"
            ),
        )
    )


def resolved(model: HuggingFaceModel, existing_id: str | None = None) -> ResolvedArtifact:
    """Turns an inspected Hub model into the draft the API returns.

    Pure: everything that needed the network happened in the client, and
    `existing_id` is a question about the catalogue that the service answers.
    """
    notes: list[ResolveNote] = []
    if not model.content_addressable:
        notes.append(
            ResolveNote(
                level=WARN,
                message=(
                    "the weights are not content-addressable, so the digest names the commit "
                    "rather than hashing the bytes"
                ),
            )
        )
        for path in model.unaddressable_files:
            notes.append(
                ResolveNote(
                    level=WARN,
                    message=f"{path} is committed to git rather than LFS and carries no content hash",
                )
            )
        if not model.weight_files:
            notes.append(ResolveNote(level=WARN, message="the repo publishes no weight files at all"))
    if model.sidecar_files:
        notes.append(
            ResolveNote(
                level=INFO,
                message=(
                    f"the served weights are the {len(model.weight_files)} file(s) at the repo root; "
                    f"{len(model.sidecar_files)} duplicate copy(s) in subdirectories were excluded from the "
                    f"size and the digest: {', '.join(model.sidecar_files)}"
                ),
            )
        )
    if model.gated:
        notes.append(
            ResolveNote(
                level=WARN,
                message="the repo is gated, so fetching the weights needs a token that has accepted its licence",
            )
        )
    if model.parameter_count is None:
        notes.append(ResolveNote(level=INFO, message="the repo does not publish a parameter count"))

    shape = extract_shape(model.config)
    active = derive_active_params(model.config, model.parameter_count)
    metadata = {
        "source": "huggingface",
        "huggingface_id": model.slug,
        "revision": model.revision,
        "quantization": native_quant_method(model.config, model.slug),
        "dtype": _dtype_of(model.config),
        "architecture": _architecture_of(model.config),
        "is_moe": is_moe(model.config),
        "active_parameter_count": active,
        "context_length": shape.context_length if shape else None,
        "shape": shape_metadata(shape) if shape else None,
    }

    recipe = suggest_recipe(model, notes)
    uri = f"hf://{model.slug}@{model.revision}"
    return ResolvedArtifact(
        reference=f"hf:{model.slug}@{model.revision}",
        digest=model.digest,
        name=model.slug.split("/")[-1],
        kind="weights",
        format=model.format,
        revision=model.revision,
        size_bytes=model.size_bytes,
        licence=model.licence,
        parameter_count=model.parameter_count,
        source_scheme=SCHEME,
        source_uri=uri,
        verified=model.content_addressable,
        metadata=metadata,
        locations=[ArtifactLocationRequest(scheme=SCHEME, uri=uri, region=None, preferred=True)],
        recipe=recipe,
        upstream=upstream_recipe(model, recipe, notes),
        existing_id=existing_id,
        notes=notes,
    )
