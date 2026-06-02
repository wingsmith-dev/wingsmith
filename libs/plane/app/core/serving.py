from __future__ import annotations

import json
import hashlib

from uuid import UUID
from typing import Any
from decimal import Decimal

from app.db.models import Artifact
from app.db.models import Workload


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


DIGEST_PREFIX = "svg_"

DIGEST_FIELDS = (
    "engine",
    "engine_version",
    "engine_image",
    "frontend_image",
    "engine_args",
    "quantization",
    "dtype",
    "tensor_parallel",
    "pipeline_parallel",
    "expert_parallel",
    "context_length",
    "max_batch_tokens",
    "kv_cache_dtype",
    "kv_cache_fraction",
    "prefix_cache_enabled",
    "prefix_cache_config",
    "device_class",
    "device_count",
    "device_memory_mib_min",
    "cpu_cores",
    "memory_mib",
    "disk_gib",
    "overlay",
    "serving",
)
"""Everything that decides what process runs, and nothing else.

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
    for name in DIGEST_FIELDS:
        payload[name] = _canonical(getattr(workload, name))
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
