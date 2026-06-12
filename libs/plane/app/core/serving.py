from __future__ import annotations

import json
import hashlib

from uuid import UUID
from typing import Any
from decimal import Decimal

from app.db.models import Artifact
from app.db.models import Workload
from app.integrations import dynamo_recipes


SERVED_MODEL_ARGS = ("served-model-name", "served_model_name")
"""The keys an override can arrive under, in the order the engine would see them.

`engine_args` holds flag names, so the hyphenated one is the real spelling: the
operator renders `--` + the key verbatim (`strings.TrimPrefix(key, "--")` in
`render/dynamo.go`) and vLLM's flag is `--served-model-name`. The underscore form
is accepted because this function looked for nothing else for a while, so it is
what anyone who read this code rather than the operator would have written — but
it does not survive the render, and a recipe carrying it gets an engine that
refuses to start on an unrecognised argument. Both are read here so that the
plane's answer never silently disagrees with the command line.

Matched against `flag_name`, never against the raw key. Real workloads store the
leading dashes — `{"--served-model-name": "moonshotai/kimi-k3"}` — because that is
what a person copying a command line writes, and the operator strips them before
rendering. Comparing the raw key to these names missed every one of those, which
is not a lookup that fails loudly: the plane fell through to the declared name,
published it on the routing table, and the gateway rewrote `model` to a string the
engine had never been told to answer to. Kimi K3 served every request as a 404
from inside the cluster with the deployment healthy and the tunnel open.
"""


def flag_name(key: str) -> str:
    """An `engine_args` key as the operator will key it: without the leading dashes.

    The one normalisation, spelled once. `render/dynamo.go` does
    `strings.TrimPrefix(key, "--")` and renders `"--" + key`, so a workload may hold
    `served-model-name` or `--served-model-name` and mean the same flag. Anything
    in the plane that reads a specific flag out of `engine_args` has to agree with
    that or it is reading a different dictionary than the engine was given.
    """
    return key.lstrip("-")


DERIVED_FLAGS = frozenset(
    {
        "model",
        "served-model-name",
        "revision",
        "max-model-len",
        "tensor-parallel-size",
        "pipeline-parallel-size",
        "gpu-memory-utilization",
        "quantization",
        "dtype",
        "kv-cache-dtype",
        "max-num-batched-tokens",
        "no-enable-prefix-caching",
    }
)
"""The flags the operator renders from typed columns, keyed as it keys them.

The mirror of `derived` in `engineArgs` (`libs/operator/internal/render/dynamo.go`),
written here because the plane has to know which flags it has already spoken for.
Every one of these comes from a column a person filled in, that `check_shape` and
`check_capacity` validated, and that placement chose devices against — so a value
for one of them arriving from anywhere else is not a default, it is a
contradiction of a decision already acted on. The operator lays `engine_args` over
its derived flags last, which means anything the plane puts in `engine_args` wins
on the command line; a suggested value for one of these would therefore silently
beat the shape the workload was placed at.

A person may still contradict any of them by hand. `engine_args` is the escape
hatch and it stays one. What this set forbids is the plane doing it on somebody's
behalf, which is why it is read by `app.integrations.dynamo_recipes` and by
nothing that handles a user's own input.
"""


def model_slug(uri: str) -> str:
    """The lowercase repository slug a location URI names. `hf://moonshotai/Kimi-K3` -> `moonshotai/kimi-k3`.

    Lowercase because that is what a caller types and what every registry treats as canonical, and
    the organisation is kept because a bare `kimi-k3` is not a model id anybody can look up.

    Spelled here and used once, by whatever stamps `Artifact.slug` on the way in. Everything that
    asks what a model is called reads the column; this is only how the column is filled. That
    separation is the fix: the same question used to be answered by re-deriving it at read time,
    which is why four call sites could disagree.
    """
    text = (uri or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    elif text.lower().startswith("hf:"):
        text = text[3:].lstrip("/")
    if "@" in text:
        text = text.rsplit("@", 1)[0]
    return text.strip("/").lower()


def artifact_slug(digest: str, locations: object) -> str:
    """The slug to stamp on an artifact, read from where it says its bytes come from.

    The preferred location wins, then the first one given. A location is the only place the
    organisation is written down — `hf://openai/gpt-oss-20b` — which is why deriving this from the
    artifact's `name` never worked: a name is `gpt-oss-20b` and no amount of reading it produces
    the `openai/`.

    An artifact adopted by watching a cluster serve it may have no location at all. It carries the
    reference in `digest` as `hf:org/model` instead, and that is read next. Empty when neither says
    anything, which the caller answers for; this returns what it can prove.
    """
    preferred, first = "", ""
    for location in locations or ():
        uri = (getattr(location, "uri", "") or "").strip()
        if not uri:
            continue
        if not first:
            first = uri
        if getattr(location, "preferred", False) and not preferred:
            preferred = uri
    if slug := model_slug(preferred or first):
        return slug
    if (digest or "").strip().lower().startswith("hf:"):
        return model_slug(digest)
    return ""


def served_model(workload: Workload, artifact: Artifact) -> str:
    """The `model_name` the engine reports itself under, most authoritative first.

    `engine_args` first, because it wins on the command line — the operator
    renders the derived flags and then lays `engine_args` over them, so anything
    decided here that it contradicts is a lie about the running process. It is the
    escape hatch for a flag this plane has never heard of and an escape hatch that
    loses to a default is not one.

    Then the artifact's slug, which is the answer in every ordinary case. It used to rank below a
    `Workload.served_model_name` and below `Recipe.name` before that, which put the fit on the
    public API surface — `kimi-k3-mxfp4-tp8-1024k` — so re-fitting the same model at a different
    tensor degree renamed the model every client codes against. Both declarations are gone: the
    slug is a fact about the weights, it survives a re-fit, and a second place to write it down is
    only a second place for it to be wrong.

    Here in `core/` rather than in a module because four of them ask the same
    question for different reasons: metrics and benchmarks narrow a series by it,
    endpoints put it on the routing table so the gateway can rewrite an inbound
    `model` to it, and the desired-state builder sends it to the cluster. It is
    machinery — the one place that knows how a serving configuration becomes the
    string an engine answers to — and a module that owned it would have the others
    importing across the domain to reach it.
    """
    args = {flag_name(key): value for key, value in (workload.engine_args or {}).items()}
    for key in SERVED_MODEL_ARGS:
        override = args.get(key)
        if override:
            return str(override)
    return declared_served_model(workload, artifact)


def declared_served_model(workload: Workload, artifact: Artifact) -> str:
    """`served_model` without the `engine_args` hatch, for what the plane declares.

    The wire carries this rather than the full answer: the operator lays
    `engine_args` over the derived flags itself, so sending the resolved hatch
    would only give the same value twice and a chance for the two to disagree.

    The workload's name remains the floor, for an artifact registered before slugs existed or one
    whose locations say nothing resolvable. It is a bad answer and it is meant to look like one —
    it is a workload's name where a model id belongs — but it is a string, and returning empty
    here renders an engine with `--served-model-name ` and a frontend that registers nothing.
    """
    return (artifact.slug or "").strip() or str(workload.name)


def recipe_engine_args(workload: Workload, artifact: Artifact) -> dict[str, str]:
    """The Dynamo recipe's defaults for these weights on this card, keyed without dashes.

    The bottom of the precedence chain and the only rung nobody declared. It exists
    because the vendor who publishes our runtime also publishes the argument list
    they run it with, per model and per node shape, and ignoring that is how a
    model that fits crash-loops anyway: `zai-org/GLM-5.3-Flash` at 1,048,576 context
    needs one Mamba state block per decode sequence, 512 of them fit, and vLLM's
    default `max_num_seqs` is 1024, so the engine refuses at CUDA graph capture
    before it serves a token.

    Read from `app.integrations.dynamo_recipes` rather than from the vLLM corpus
    because the cluster runs `python3 -m dynamo.vllm` and not `vllm serve`. The
    allow-list of what may be carried across lives there, next to the reasons for
    everything left out of it.

    `DERIVED_FLAGS` is subtracted here as well as being kept out of the allow-list,
    because this is the seam where a recipe value would actually reach a command
    line. The two guards say the same thing in the two places it has to be true:
    one is a rule about what may be adopted, and this is the last chance to stop a
    suggestion from overruling a shape the workload was placed at.

    Empty for every model the corpus does not cover, which is nearly all of them.
    """
    defaults = dynamo_recipes.engine_defaults(artifact.slug or "", workload.engine, workload.device_class)
    return {flag: value for flag, value in defaults.items() if flag not in DERIVED_FLAGS}


DIGEST_PREFIX = "svg_"

DIGEST_FIELDS: tuple[tuple[str, str], ...] = (
    ("engine", "engine"),
    ("engine_version", "engine_runtime_version"),
    ("engine_image", "engine_runtime_image"),
    ("frontend_image", "frontend_image"),
    ("engine_args", "engine_args"),
    ("quantization", "quantization"),
    ("dtype", "dtype"),
    ("tensor_parallel", "tensor_parallel"),
    ("pipeline_parallel", "pipeline_parallel"),
    ("expert_parallel", "expert_parallel"),
    ("context_length", "context_length"),
    ("max_batch_tokens", "max_batch_tokens"),
    ("kv_cache_dtype", "kv_cache_dtype"),
    ("kv_cache_fraction", "kv_cache_fraction"),
    ("prefix_cache_enabled", "prefix_cache_enabled"),
    ("prefix_cache_config", "prefix_cache_config"),
    ("device_class", "device_class"),
    ("device_count", "device_count"),
    ("device_memory_mib_min", "device_memory_mib_min"),
    ("cpu_cores", "cpu_cores"),
    ("memory_mib", "memory_mib"),
    ("disk_gib", "disk_gib"),
    ("overlay", "overlay"),
    ("serving", "serving"),
)
"""Everything that decides what process runs, and nothing else.

`(hash key, attribute)`, and the two differ where a column has since been renamed.
The hash key is frozen at whatever it was the day the field entered the digest,
because the digest is an identity: `ops_benchmark_run.serving_digest` names the
configuration a measurement was taken against, and a run whose digest no longer
matches any workload has lost the only link back to what it measured. Renaming a
column is a statement about how this code reads; it is not a statement that the
configuration changed, and it must not be recorded as one. So `engine_runtime_version`
still hashes under `engine_version`, and every digest written before the rename is
still the digest of the same configuration after it.

Read in this order and hashed with the artifact, which is the one field not named
here because it is an identity rather than a column value.

What is deliberately absent is the whole point of the field. `imported_args` is
observed — it was read off somebody else's running process and is never rendered,
so two workloads that differ only there ask for the same engine. `capabilities`
is asserted and never reaches a cluster. `served_model_name` is a name rather
than a footprint: the same engine under two names is one configuration served
twice, which is exactly the canary case the recipe's sharing existed for. Scale,
placement, priority and labels are not properties of the configuration at all.
"""


def serving_digest(workload: Workload, artifact: Artifact) -> str:
    """`svg_` + sha256 over the fields that decide what process runs.

    The identity of a serving configuration is its content, and this is that
    statement made literal. The console used to reconstruct it by comparing eight
    fields to decide whether an existing recipe could be reused
    (`matchExistingRecipe`); comparing contents to find an object is the tell that
    the contents are the object.

    Canonical JSON — sorted keys, no whitespace — so that two workloads written
    months apart by different code paths hash the same. `Decimal` and `UUID` are
    coerced to `str` rather than left to a JSON encoder that would refuse them,
    and `kv_cache_fraction` therefore hashes as the string Postgres round-trips
    rather than as a float whose repr depends on the driver.

    Derived on every write and never accepted from a client: a digest somebody
    else computed is a claim about a configuration rather than a reading of it.
    """
    payload: dict[str, Any] = {"artifact": str(artifact.uid)}
    for key, attribute in DIGEST_FIELDS:
        payload[key] = _canonical(getattr(workload, attribute))
    # Only when the recipe actually supplies something, so that the digest of a model the
    # corpus does not cover is byte-identical to the one this function returned before
    # recipe defaults existed. An absent key and an empty map are different hashes, and
    # every workload in the fleet re-rendering once is the cost of confusing them.
    defaults = recipe_engine_args(workload, artifact)
    if defaults:
        payload["engine_defaults"] = _canonical(defaults)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return DIGEST_PREFIX + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    """A column value as something `json.dumps` will accept and always spell the same."""
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value
