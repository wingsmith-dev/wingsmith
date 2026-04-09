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

from app.core.schemas import Struct
from app.modules.recipes.vram import MIN_CONTEXT_LENGTH
from app.modules.recipes.vram import DEFAULT_QUANTIZATION
from app.modules.recipes.vram import is_moe
from app.modules.recipes.vram import extract_shape
from app.modules.recipes.vram import effective_config
from app.modules.recipes.vram import fit_context_length
from app.modules.recipes.vram import native_quant_method
from app.modules.recipes.vram import derive_active_params
from app.modules.recipes.vram import baseline_min_gpu_memory_gb
from app.integrations.huggingface import HuggingFaceModel

from .schemas import ArtifactLocationRequest


SCHEME = "hf"

_MAX_DEVICE_MEMORY_GB = 141
"""The largest single accelerator we operate. Past this, a suggestion has to shard."""

_MIB_PER_GB = 1024


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
    notes: list[str]


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


def suggest_recipe(model: HuggingFaceModel, notes: list[str]) -> RecipeDefaults:
    """Sizes a first recipe from the weights, sharding only when one device cannot hold them.

    Quantization is resolved from the repo rather than defaulted: a repo shipping
    mxfp4 sized as fp16 overcounts weights fourfold, which turns a suggestion into
    a placement that never schedules.
    """
    quantization = native_quant_method(model.config, model.slug) or DEFAULT_QUANTIZATION
    dtype = _dtype_of(model.config)
    shape = extract_shape(model.config)

    if shape is None or not model.parameter_count:
        notes.append("the config did not carry an attention shape, so no device sizing is suggested")
        return RecipeDefaults(
            device_count=1,
            device_memory_mib_min=0,
            context_length=None,
            quantization=quantization,
            dtype=dtype,
        )

    floor_gb = baseline_min_gpu_memory_gb(
        total_params=model.parameter_count,
        shape=shape,
        quantization=quantization,
    )
    device_count = max(1, math.ceil(floor_gb / _MAX_DEVICE_MEMORY_GB))
    per_device_gb = math.ceil(floor_gb / device_count)
    if device_count > 1:
        notes.append(f"the weights do not fit one device, so the suggestion shards across {device_count}")

    fit = fit_context_length(
        total_params=model.parameter_count,
        shape=shape,
        quantization=quantization,
        declared_context_length=shape.context_length or MIN_CONTEXT_LENGTH,
        concurrency=1,
        available_gb=float(device_count * per_device_gb),
    )
    if fit.reduced:
        notes.append(
            f"the declared context of {shape.context_length} does not fit the suggested devices, "
            f"so {fit.context_length} is offered instead"
        )

    return RecipeDefaults(
        device_count=device_count,
        device_memory_mib_min=per_device_gb * _MIB_PER_GB,
        context_length=fit.context_length,
        quantization=quantization,
        dtype=dtype,
    )


def resolved(model: HuggingFaceModel, existing_id: str | None = None) -> ResolvedArtifact:
    """Turns an inspected Hub model into the draft the API returns.

    Pure: everything that needed the network happened in the client, and
    `existing_id` is a question about the catalogue that the service answers.
    """
    notes: list[str] = []
    if not model.content_addressable:
        notes.append(
            "the weights are not content-addressable, so the digest names the commit rather than hashing the bytes"
        )
        for path in model.unaddressable_files:
            notes.append(f"{path} is committed to git rather than LFS and carries no content hash")
        if not model.weight_files:
            notes.append("the repo publishes no weight files at all")
    if model.sidecar_files:
        notes.append(
            f"{len(model.sidecar_files)} file(s) under a subdirectory were read as extra copies rather than "
            f"as the served weights, and count towards neither the size nor the digest: "
            f"{', '.join(model.sidecar_files)}"
        )
    if model.gated:
        notes.append("the repo is gated, so fetching the weights needs a token that has accepted its licence")
    if model.parameter_count is None:
        notes.append("the repo does not publish a parameter count")

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
