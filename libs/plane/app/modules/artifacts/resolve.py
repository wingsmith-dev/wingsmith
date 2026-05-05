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
        recipe=suggest_recipe(model, notes),
        existing_id=existing_id,
        notes=notes,
    )
