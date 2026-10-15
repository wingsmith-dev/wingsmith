"""The serving block: how a request becomes stored columns, and how stored columns read back.

One reading of the nested request shape in each direction, so that what is validated,
what is stored, what is hashed and what a read returns are the same configuration.
"""

from __future__ import annotations

import json

from decimal import Decimal
from collections.abc import Sequence

import msgspec

from app.config import settings
from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import WorkloadRole
from app.core.serving import TEXT
from app.core.serving import AGGREGATED
from app.core.serving import is_omni
from app.core.serving import serving_digest
from app.core.serving import declared_router
from app.core.serving import recipe_engine_args

from .schemas import RoleSchema
from .schemas import CacheSchema
from .schemas import ShapeSchema
from .schemas import RouterSchema
from .schemas import OverlaySchema
from .schemas import ServingSchema
from .schemas import TopologySchema
from .schemas import ResourcesSchema
from .schemas import ParallelismSchema
from .schemas import CapabilitiesSchema
from .schemas import TopologyViewSchema
from .schemas import WorkloadServingSchema
from .schemas import WorkloadServingRequest


SERVING_FLAGS: dict[str, dict[str, str]] = {
    "vllm": {
        "parser": "reasoning-parser",
        "max_images_per_request": "limit-mm-per-prompt",
        "max_videos_per_request": "limit-mm-per-prompt",
    },
    "sglang": {
        "parser": "reasoning-parser",
        "tool_call_parser": "tool-call-parser",
        "structured_output_backend": "grammar-backend",
    },
}
"""The engine argument each typed serving field speaks for, per engine.

Keyed without the leading dashes, the way the operator keys them once it has
trimmed them. Only the fields that genuinely map to one flag are here: an engine
this plane has never heard of maps nothing, and neither `context_overflow` nor
EOS suppression is a single flag on every engine — those are the operator's to
render, and it is entitled to fail a reconcile rather than drop one silently.

**These are the flags of the program the cluster actually runs, which for vLLM is
not `vllm serve`.** Dynamo runs `python3 -m dynamo.vllm`, the engine, and
`python3 -m dynamo.frontend`, the OpenAI surface — two programs with two CLIs, and
neither of them is vLLM's standalone server. `--tool-call-parser` and
`--enable-auto-tool-choice` are that server's flags and exist on neither, so
emitting them did not misconfigure tool calling: the worker exited on
`unrecognized arguments` before it loaded a weight, every restart, for every
workload that named a parser. `--guided-decoding-backend` is the same flag from
the same program, and this vLLM has renamed its equivalent to
`--structured-outputs-config`, which takes a config object rather than a backend
name — a different field, not a renamed one.

What is left is what was checked against the running containers' own `--help`:
`--reasoning-parser` and `--limit-mm-per-prompt` are engine arguments and the
worker takes both. sglang keeps its entries because sglang's server is the program
its operator runs.

Tool-call parsing under Dynamo is the chat processor's job — `--dyn-chat-processor`
on the frontend, whose Rust preprocessor derives the parser from the model — and
wiring it is a separate change against Dynamo's own configuration rather than one
more row here. The field stays on the workload and records the intent. Where nothing
renders it, `hashed_serving` leaves it out of `serving_digest`, so setting it neither
names a new configuration nor restarts a worker."""


FLAG_ONLY_FIELDS = ("tool_call_parser", "structured_output_backend")
"""Serving fields whose only effect is the engine flag `SERVING_FLAGS` maps them to.

Where the engine's table has no row for one, the field reaches no command line, and
hashing it would make a recorded intent look like a different running process.
"""


def hashed_serving(engine: str, stored: dict | None) -> dict:
    """The stored serving block as `serving_digest` should see it: what nothing renders reads as unset.

    Unset rather than removed, so a workload that never named the field hashes exactly
    as it did before: an absent key and a null one are different digests.
    """
    flags = SERVING_FLAGS.get((engine or "").strip().lower(), {})
    return {
        key: None if key in FLAG_ONLY_FIELDS and key not in flags else value for key, value in (stored or {}).items()
    }


MODALITY_LIMITS: dict[str, str] = {
    "max_images_per_request": "image",
    "max_videos_per_request": "video",
}
"""The per-modality caps, and the key each one holds inside `--limit-mm-per-prompt`.

Two typed fields and one flag. vLLM takes the whole map at once, so a workload that caps
both renders `{"image": 4, "video": 1}` and never two flags — which is why the caps are
merged here rather than assigned one at a time in `serving_args`.

An unset cap is not "no limit": vLLM reads every modality absent from the map as zero, so
a workload declaring `video` and leaving `max_videos_per_request` at `None` renders
`{"image": N}` and is refused the exact modality it claims. `check_serving` therefore
requires the cap in both directions — a cap needs its modality, and a modality that has a
cap field needs its cap. Only `image` and `video` are keys here, so `text` and `audio` are
untouched by that rule.

Named per modality rather than as one generic `max_attachments` because the limits do not
generalise: an audio limit is seconds, not a count, and a generic field would need a unit
it cannot have.

Video arrives with nothing else attached. A video is a `video_url` content part on the
ordinary chat body — an https URL or a `data:video/mp4;base64,...` one — so the gateway
routes it today on the JSON path, and the cap is the whole of what the plane has to
declare. What that costs is `maxRequestBodyBytes`
(`libs/gateway/internal/serving/serving.go`): 32 MiB of buffered body, and base64 is a
third larger than the bytes it carries, so an inline video has about 24 MiB to fit in.
An https URL has no such ceiling and is the shape to prefer above a few seconds.
"""


SERVING_IMPLIED_FLAGS: dict[str, dict[str, tuple[str, ...]]] = {
    "vllm": {
        "max_images_per_request": ("enable-multimodal",),
        "max_videos_per_request": ("enable-multimodal",),
    },
}
"""Valueless flags a typed field cannot live without, per engine.

The rule for adding a row: the flag has to appear in the `--help` of the program the
cluster actually starts. A flag the engine's upstream documents is not the same claim.
This table was empty for a while because its only entry broke that rule — `vllm serve`
parses tool calls with `--tool-call-parser` and dispatches them only with
`--enable-auto-tool-choice`, so the pair genuinely travels together, but Dynamo runs
`python3 -m dynamo.vllm` and takes neither, which made the row a guaranteed crash
rather than a completed configuration. See `SERVING_FLAGS`.

`--enable-multimodal` meets the rule: it is declared in
`dynamo.vllm.backend_args.DynamoVllmArgGroup` as `DYN_VLLM_ENABLE_MULTIMODAL`, and its
own help says what happens without it — *"if not set, none of the multimodal components
can be used"*. So `--limit-mm-per-prompt` alone is a cap on a path that is switched off,
which is what shipped: a workload declaring `image` in `capabilities.input_modalities`
recorded the claim, rendered the cap, and served a text-only worker.

It hangs off the typed caps in `MODALITY_LIMITS` and not off the capability block on purpose.
`capabilities` is asserted, reaches no cluster and is deliberately absent from
`serving_digest` (`app.core.serving.SERVING_DIGEST_FIELDS`) — deriving a flag from it
would make two workloads with one digest run two different processes. The typed field is
the one that is hashed, and `check_serving` already refuses a cap unless its own modality is
among the declared ones, so setting it is the same declaration made where it counts."""


def stored_serving(serving: ServingSchema) -> dict:
    """The typed serving block as the `serving` column holds it.

    `output_modality` and `diffusion` are left out when unset, and `text` is stored as unset.
    The column is hashed whole into `serving_digest`, so a key that every write started adding
    would move the digest of every chat workload the next time it was saved — and with it every
    deployment's rendered annotation and every benchmark's link to what it measured.
    """
    stored = msgspec.to_builtins(serving)
    if stored.get("output_modality") in (None, TEXT):
        stored.pop("output_modality", None)
    if stored.get("diffusion") is None:
        stored.pop("diffusion", None)
    return stored


def serving_args(engine: str, serving: ServingSchema) -> dict[str, str]:
    """The engine arguments the typed serving block speaks for, keyed without dashes."""
    flags = SERVING_FLAGS.get(engine.strip().lower(), {})
    args: dict[str, str] = {}
    for field_name, value in (
        ("parser", serving.reasoning.parser),
        ("tool_call_parser", serving.tool_call_parser),
        ("structured_output_backend", serving.structured_output_backend),
    ):
        if value and field_name in flags:
            args[flags[field_name]] = value
    limits: dict[str, dict[str, int]] = {}
    for field_name, modality in MODALITY_LIMITS.items():
        limit = getattr(serving, field_name)
        if limit is None or field_name not in flags:
            continue
        limits.setdefault(flags[field_name], {})[modality] = limit
    for flag, per_modality in limits.items():
        args[flag] = json.dumps(dict(sorted(per_modality.items())))
    return args


def serving_companion_args(engine: str, serving: ServingSchema) -> dict[str, str]:
    """The valueless flags a typed field cannot work without, keyed without dashes.

    Separate from `serving_args` because the precedence is the other way round. A
    typed field beats `engine_args`, since it is the value the plane can validate and
    display. A companion flag does not: it is inferred rather than declared, so a
    workload that spells it out by hand — for an engine build that wants a value there,
    or to hold a flag this table gets wrong — keeps what it wrote.
    """
    implied = SERVING_IMPLIED_FLAGS.get(engine.strip().lower(), {})
    args: dict[str, str] = {}
    for field_name in MODALITY_LIMITS:
        if getattr(serving, field_name) is None:
            continue
        for flag in implied.get(field_name, ()):
            args[flag] = ""
    return args


def engine_runtime_repos() -> dict[str, str]:
    """`ENGINE_RUNTIME_REPOS` parsed into `engine -> repository`.

    Malformed pairs are dropped rather than raised on: this is read on the write
    path of every workload, and one bad character in an environment variable must
    not be the reason a workload cannot be created.
    """
    repos: dict[str, str] = {}
    for pair in settings.ENGINE_RUNTIME_REPOS.split(","):
        engine, _, repository = pair.strip().partition("=")
        if engine and repository:
            repos[engine.strip().lower()] = repository.strip()
    return repos


def resolve_engine_runtime_image(engine: str, engine_runtime_version: str, declared: str | None) -> str | None:
    """The image a workload serves with, resolved from its engine and runtime version.

    The engine picks the repository and the runtime version is the tag, because a
    Dynamo runtime image is exactly those two things: `vllm-runtime` is the build
    with vLLM and `dynamo.vllm` in it, and `1.4.1` is which Dynamo release. The
    engine's own version appears nowhere and cannot — which vLLM sits inside
    `vllm-runtime:1.4.1` is NVIDIA's business and is not on any wire here.

    A declared image is returned untouched — pinning an exact image is the whole
    reason the field is writable, and a workload that names one has already said
    what it means. Everything else is the common case: an author picks `vllm` and
    a runtime version, and the repository those two imply is configuration, not a
    thing to retype.

    Returns `None` for an engine with no configured repository, which leaves the
    workload exactly as it was before this existed. That is deliberate: inventing an
    address for an engine the plane has never been told about would trade a
    visible "no image" for an `ImagePullBackOff` on a cluster.

    The omni engine borrows vLLM's repository unless it has one of its own: Dynamo ships
    `dynamo.vllm.omni` inside `vllm-runtime`, so it is the same image with a different module.
    """
    if declared:
        return declared
    key = (engine or "").strip().lower()
    repos = engine_runtime_repos()
    repository = repos.get(key) or (repos.get("vllm") if is_omni(key) else None)
    if not repository:
        return None
    tag = (engine_runtime_version or "").strip()
    if not tag or tag == "latest":
        tag = settings.ENGINE_RUNTIME_TAG_DEFAULT
    return f"{repository}:{tag}"


def resolved_engine_args(workload: Workload, artifact: Artifact) -> dict[str, str]:
    """The engine arguments this workload asks for, most authoritative last.

    Four rungs, and the order is the whole of the function:

    1. **The recipe's defaults**, for a flag NVIDIA publishes for these weights and
       this plane has no column for. Nobody declared them, so they lose to everything.
    2. **Companion flags** a typed field cannot work without — inferred, so they
       lose to anything written by hand.
    3. **`engine_args`**, the workload's own escape hatch. It beats both of the
       above because a person typed it, and an escape hatch that loses to a default
       is not one.
    4. **The typed serving flags.** A flag that *is* modelled has one declared home,
       and a workload that pins `--reasoning-parser` by hand while also naming
       `serving.reasoning.parser` is stating the same thing twice — the typed value
       is the one the plane can validate, display and verify.

    What is *not* a rung is the plane's own derived fields — context length, tensor
    parallelism, the memory fraction. They reach the cluster from typed columns that
    were validated and placed against, and the operator lays this map over them
    last, so anything here beats them on the command line. A recipe is therefore
    never allowed to name one: `dynamo_recipes.ADOPTED_FLAGS` is filtered against
    `app.core.serving.DERIVED_FLAGS`, and a recipe stating a context length or a
    parallel size is read as evidence and applied to nothing.

    Derived on every read and stored nowhere: `engine_args` is left exactly as it
    was written.

    TODO: the operator renders rung 4 the other way round today — `engineArgs`
    (`libs/operator/internal/render/dynamo.go`) lays `engine_args` over its own
    derived flags last, so whichever way the serving block eventually reaches the
    wire, the two have to be made to agree or this becomes a resolved view of
    something the cluster does not do.
    """
    serving = serving_block(workload.serving)
    resolved = dict(recipe_engine_args(workload, artifact))
    for flag, value in serving_companion_args(workload.engine, serving).items():
        resolved.setdefault(flag, value)
    resolved.update({str(key).removeprefix("--"): str(value) for key, value in (workload.engine_args or {}).items()})
    resolved.update(serving_args(workload.engine, serving))
    return resolved


def serving_block(stored: dict | None) -> ServingSchema:
    return msgspec.convert(stored or {}, ServingSchema)


def router_block(stored: dict | None) -> RouterSchema:
    return msgspec.convert(declared_router(stored), RouterSchema)


def capabilities_block(stored: dict | None) -> CapabilitiesSchema:
    return msgspec.convert(stored or {}, CapabilitiesSchema)


FRACTION_SCALE = Decimal("0.0001")
"""The scale `demand_workload.kv_cache_fraction` is stored at, as `Numeric(5, 4)`.

Written through on the way in so that the value a create holds in memory is the
value Postgres hands back on the next read. The digest hashes this column as the
string it round-trips as, so a `0.9` that stayed a float until the next update
would hash differently from the `0.9000` the database returned — the same
configuration with two identities, produced by nothing but a reload.
"""


def _fraction(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(FRACTION_SCALE)


def group_devices(device_count: int, roles: Sequence[WorkloadRole]) -> int:
    """Devices one indivisible unit costs: the sum of `weight x device_count` over the roles.

    Derived here and stored nowhere, which is the point. `device_count` is a `serving_digest`
    field and it means **devices per worker** — 8 at TP8, never the group's 24. A column holding
    the group total would put the role weights into the hash through a second door, silently,
    because nothing about such a column's name would say which of the two it held.

    An aggregated workload's group is one worker, so this is its `device_count` and the function
    is true of both topologies rather than being a disaggregation special case.
    """
    if not roles:
        return device_count
    return sum(max(role.weight, 0) * device_count for role in roles)


def topology_view(workload: Workload, roles: Sequence[WorkloadRole]) -> TopologyViewSchema:
    """The topology block as a read returns it, with the derived numbers filled in.

    Takes the roles rather than reading `workload.roles`, for the reason `serving_digest` gives at
    length: the relationship is `lazy="noload"` and a noloaded collection answers an empty list
    rather than raising, so a view that read the attribute would quietly report an aggregated
    workload wherever the caller had not asked for the rows.
    """
    group = group_devices(workload.device_count, roles)
    return TopologyViewSchema(
        serving_topology=workload.serving_topology or AGGREGATED,
        roles=[
            RoleSchema(
                role=role.role,
                weight=role.weight,
                max_batch_tokens=role.max_batch_tokens,
                engine_args=role.engine_args or {},
            )
            for role in roles
        ],
        group_devices=group,
    )


def declared_roles(topology: TopologySchema) -> list[WorkloadRole]:
    """The request's role block as the rows that are stored, validated and hashed.

    One conversion, used by validation, by the write and by the digest, so that the thing refused
    is the thing stored and the thing stored is the thing hashed. `apply_serving` computes the
    digest from the row after the columns have landed on it for the same reason, and a second
    reading of the request here would reintroduce exactly the gap that comment closes.

    Detached rows: no `workload_id`, because the caller decides whether these replace an existing
    workload's roles or belong to one that does not exist yet.
    """
    return [
        WorkloadRole(
            role=role.role,
            weight=role.weight,
            max_batch_tokens=role.max_batch_tokens,
            engine_args=role.engine_args or {},
        )
        for role in topology.roles or ()
    ]


def serving_columns(data: WorkloadServingRequest, artifact: Artifact) -> dict:
    """A validated serving block as the columns it is stored in.

    The one place the nested request shape becomes flat columns, so create,
    promotion and update all write the same fields from the same reading of them.
    `serving_digest` is not here: it is derived from the row after these have
    landed on it, by `app.core.serving`, which is the only thing entitled to
    compute it.
    """
    return {
        "artifact_id": artifact.id,
        "engine": data.engine,
        "engine_runtime_version": data.engine_runtime_version,
        "engine_runtime_image": resolve_engine_runtime_image(
            data.engine, data.engine_runtime_version, data.engine_runtime_image
        ),
        "frontend_image": data.frontend_image,
        "engine_args": data.engine_args,
        "imported_args": data.imported_args,
        "quantization": data.quantization,
        "dtype": data.dtype,
        "tensor_parallel": data.parallelism.tensor,
        "pipeline_parallel": data.parallelism.pipeline,
        "expert_parallel": data.parallelism.expert,
        "context_length": data.context_length,
        "max_batch_tokens": data.max_batch_tokens,
        "kv_cache_dtype": data.cache.kv_cache_dtype,
        "kv_cache_fraction": _fraction(data.cache.kv_cache_fraction),
        "prefix_cache_enabled": data.cache.prefix_cache_enabled,
        "prefix_cache_config": data.cache.prefix_cache_config,
        "device_class": data.shape.device_class,
        "device_count": data.shape.device_count,
        "device_memory_mib_min": data.shape.device_memory_mib_min,
        "requires_single_topology_domain": data.shape.requires_single_topology_domain,
        "topology_domain_kind": data.shape.topology_domain_kind,
        "cpu_cores": data.resources.cpu_cores,
        "memory_mib": data.resources.memory_mib,
        "disk_gib": data.resources.disk_gib,
        "overlay": msgspec.to_builtins(data.overlay),
        "serving": stored_serving(data.serving),
        "capabilities": msgspec.to_builtins(data.capabilities),
        "router": declared_router(msgspec.to_builtins(data.router)),
    }


def serving_request(
    workload: Workload, artifact: Artifact, roles: Sequence[WorkloadRole] = ()
) -> WorkloadServingRequest:
    """A stored workload read back as the request that would have created it.

    A patch is applied to this and the result validated whole, which is what keeps
    one description of a legal configuration rather than one for creates and a
    weaker one for updates.
    """
    fraction = workload.kv_cache_fraction
    return WorkloadServingRequest(
        artifact=artifact.uid,
        engine=workload.engine,
        engine_runtime_version=workload.engine_runtime_version,
        engine_runtime_image=workload.engine_runtime_image,
        frontend_image=workload.frontend_image,
        engine_args=workload.engine_args,
        imported_args=workload.imported_args,
        quantization=workload.quantization,
        dtype=workload.dtype,
        parallelism=ParallelismSchema(
            tensor=workload.tensor_parallel,
            pipeline=workload.pipeline_parallel,
            expert=workload.expert_parallel,
        ),
        context_length=workload.context_length,
        max_batch_tokens=workload.max_batch_tokens,
        cache=CacheSchema(
            kv_cache_dtype=workload.kv_cache_dtype,
            kv_cache_fraction=None if fraction is None else float(fraction),
            prefix_cache_enabled=workload.prefix_cache_enabled,
            prefix_cache_config=workload.prefix_cache_config,
        ),
        shape=ShapeSchema(
            device_class=workload.device_class,
            device_count=workload.device_count,
            device_memory_mib_min=workload.device_memory_mib_min,
            requires_single_topology_domain=workload.requires_single_topology_domain,
            topology_domain_kind=workload.topology_domain_kind,
        ),
        resources=ResourcesSchema(
            cpu_cores=workload.cpu_cores,
            memory_mib=workload.memory_mib,
            disk_gib=workload.disk_gib,
        ),
        overlay=OverlaySchema(
            pod_spec=(workload.overlay or {}).get("pod_spec", {}) or {},
            dynamo=(workload.overlay or {}).get("dynamo", {}) or {},
        ),
        serving=serving_block(workload.serving),
        capabilities=capabilities_block(workload.capabilities),
        router=router_block(workload.router),
        topology=TopologySchema(
            serving_topology=workload.serving_topology or AGGREGATED,
            roles=[
                RoleSchema(
                    role=role.role,
                    weight=role.weight,
                    max_batch_tokens=role.max_batch_tokens,
                    engine_args=role.engine_args or {},
                )
                for role in roles
            ],
        ),
    )


def serving_view(workload: Workload, artifact: Artifact, roles: Sequence[WorkloadRole] = ()) -> WorkloadServingSchema:
    """The serving block as a read returns it, with the resolved arguments beside it.

    Takes the artifact and not its id, because `resolved_engine_args` reads the
    slug off it to find the model's recipe. A read that showed different resolved
    arguments from the ones the wire carries would be worse than showing none.
    """
    fraction = workload.kv_cache_fraction
    serving = serving_block(workload.serving)
    return WorkloadServingSchema(
        artifact_id=artifact.uid,
        engine=workload.engine,
        engine_runtime_version=workload.engine_runtime_version,
        engine_runtime_image=workload.engine_runtime_image,
        frontend_image=workload.frontend_image,
        engine_args=workload.engine_args,
        imported_args=workload.imported_args,
        quantization=workload.quantization,
        dtype=workload.dtype,
        parallelism=ParallelismSchema(
            tensor=workload.tensor_parallel,
            pipeline=workload.pipeline_parallel,
            expert=workload.expert_parallel,
        ),
        context_length=workload.context_length,
        max_batch_tokens=workload.max_batch_tokens,
        cache=CacheSchema(
            kv_cache_dtype=workload.kv_cache_dtype,
            kv_cache_fraction=None if fraction is None else float(fraction),
            prefix_cache_enabled=workload.prefix_cache_enabled,
            prefix_cache_config=workload.prefix_cache_config,
        ),
        serving=serving,
        capabilities=capabilities_block(workload.capabilities),
        resolved_engine_args=resolved_engine_args(workload, artifact),
        shape=ShapeSchema(
            device_class=workload.device_class,
            device_count=workload.device_count,
            device_memory_mib_min=workload.device_memory_mib_min,
            requires_single_topology_domain=workload.requires_single_topology_domain,
            topology_domain_kind=workload.topology_domain_kind,
        ),
        resources=ResourcesSchema(
            cpu_cores=workload.cpu_cores,
            memory_mib=workload.memory_mib,
            disk_gib=workload.disk_gib,
        ),
        overlay=OverlaySchema(
            pod_spec=(workload.overlay or {}).get("pod_spec", {}) or {},
            dynamo=(workload.overlay or {}).get("dynamo", {}) or {},
        ),
        topology=topology_view(workload, roles),
        router=router_block(workload.router),
    )


def apply_serving(
    workload: Workload, data: WorkloadServingRequest, artifact: Artifact, roles: Sequence[WorkloadRole]
) -> None:
    """Writes a validated serving configuration onto a workload, digest included.

    The digest is computed from the row after the columns have landed on it rather
    than from the request, so what is hashed is what was stored — a coercion the
    write performs and the hash did not see would be a digest that names a
    configuration nobody is running.

    `roles` is passed through rather than read off `workload.roles`, which is `lazy="noload"`
    and answers an empty list rather than raising. `serving_digest` states the consequence: a
    noloaded collection would hash a disaggregated workload as having no roles, silently. It is
    the one seam a role set arrives through: promotion passes an empty one because what it
    adopted is a running process rather than a declared shape, and every declaring caller passes
    what `declared_roles` read off the request it validated.
    """
    for key, value in serving_columns(data, artifact).items():
        setattr(workload, key, value)
    workload.serving_topology = data.topology.serving_topology or AGGREGATED
    workload.serving_digest = serving_digest(
        workload, artifact, roles, serving=hashed_serving(workload.engine, workload.serving)
    )


def serving_snapshot(workload: Workload, artifact: Artifact, roles: Sequence[WorkloadRole]) -> dict:
    """A stored workload's serving configuration as the patch that would restate it.

    `serving_request` reads the workload back as the request that would have *created* it, which
    carries one field a patch may not: `artifact`. The weights are not patchable — repointing a
    workload at other weights makes every deployment expanded from it mean something else — so
    the snapshot a revision keeps is that reading minus the artifact, and the artifact is
    recorded beside it instead.

    Popping rather than rebuilding the dict by hand on purpose. There is one description of a
    serving configuration in this module and it is `WorkloadServingRequest`; a second list of
    its fields spelled out here would be a field added to one and forgotten in the other.
    """
    snapshot = msgspec.to_builtins(serving_request(workload, artifact, roles))
    snapshot.pop("artifact", None)
    return snapshot
