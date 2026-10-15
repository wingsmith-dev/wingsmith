from __future__ import annotations

import json
import hashlib

from uuid import UUID
from typing import Any
from decimal import Decimal
from collections.abc import Sequence

from app.db.models import Artifact
from app.db.models import Workload
from app.db.models import WorkloadRole
from app.integrations import dynamo_recipes
from app.core.exceptions import Invalid


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


AGGREGATED = "aggregated"
DISAGGREGATED = "disaggregated"
SERVING_TOPOLOGIES = frozenset({AGGREGATED, DISAGGREGATED})
"""The two ways a workload's weights can be served, and there is no third.

Spelled here rather than in the workloads module because `serving_digest` branches on the
value and the module validates it, and a closed set with two readers and one spelling is the
only version of it that cannot drift. `Workload.serving_topology` names the column and says
why it is not called `topology`.
"""

OMNI_ENGINES = frozenset({"omni", "vllm-omni", "vllm_omni"})
"""The spellings of the vLLM-Omni engine, which Dynamo runs as `python3 -m dynamo.vllm.omni`.

The operator accepts the same three (`engineModule` in `libs/operator/internal/render/dynamo.go`).
It is the one engine here that emits images and video rather than text, runs inside the
`vllm-runtime` image, and has no prefill/decode split.
"""

TEXT = "text"
OUTPUT_MODALITIES = frozenset({TEXT, "image", "video"})
"""What a workload's engine emits. `text` is every engine but omni; `image` and `video` are omni's.

A null `serving.output_modality` means text, and is how every workload declared before the field
existed reads. The routing table carries the resolved value so the gateway can refuse a path the
upstream does not answer.
"""


AMD64_ONLY_ENGINES = OMNI_ENGINES
"""Engines whose runtime images are built for x86-64 hosts only.

vLLM-Omni is the member: Dynamo's omni path has no arm64 build, so a Grace device class
(`host_architecture: arm64` in `libs/packages/contract/devices.json`) cannot serve it.
"""


def is_omni(engine: str | None) -> bool:
    """Whether an engine name is the vLLM-Omni diffusion engine, in any accepted spelling."""
    return (engine or "").strip().lower() in OMNI_ENGINES


def output_modality(serving: dict | None) -> str:
    """The output modality a stored `serving` block declares, `text` when it declares none."""
    return str((serving or {}).get("output_modality") or TEXT)


PREFILL = "prefill"
DECODE = "decode"
SERVING_ROLES = frozenset({PREFILL, DECODE})
"""The roles a disaggregated workload has, and it has both or it is not one.

Closed for the reason `check_shape` gives about device classes: a role outside the set renders
a `--disaggregation-mode` no engine accepts, so free text turns a validation error somebody
can read into a worker that will not start.
"""

TOPOLOGY_FLAGS = frozenset({"disaggregation-mode", "kv-transfer-config"})
"""The two flags the topology decides, keyed as the operator keys them.

`DERIVED_FLAGS` is the same statement about the fields a person filled in; these two are
derived from `serving_topology` and the role instead, which is a decision nobody typed and
therefore one nobody may contradict from `engine_args`. `kv-transfer-config` is the same
string on both roles — the transfer mechanism is a property of the topology, not of the role
— and `disaggregation-mode` is the role itself, so a value for either arriving from a role's
own arguments is a second answer to a question already settled.
"""

CONTENT_LOGGING_FLAGS = frozenset({"enable-log-requests", "enable-log-outputs", "log-requests", "log-requests-level"})
"""Engine flags that write a request's prompt or its completion to the worker's log.

Refused on every workload, at the deployment level and on a role, because zero
retention of request content is a promise about the whole serving path and a
worker's stdout is kept by whatever log pipeline the cluster runs. vLLM and
SGLang both default these off, so `engine_args` is the only way one arrives. The
operator removes them again at render (`contentLoggingFlags` in
`libs/operator/internal/render/dynamo.go`) for any row written before this check.
"""

LOG_LEVEL_FLAGS = frozenset({"log-level"})
"""Engine flags that set a log level, refused at a level that logs request content.

SGLang's `--log-level`. At debug its tool-call detectors log the model's output
text (`function_call/gigachat3_detector.py`, `entrypoints/openai/serving_chat.py`
in 0.5.21), so a debug worker writes completions to its log with no
content-logging flag set.
"""

VERBOSE_LEVELS = frozenset({"debug", "trace", "notset"})
"""Log levels at which some engine on the serving path writes request content.

Checked against the engine sources. Dynamo's frontend logs every chat,
completions and responses request whole at `trace` (`lib/llm/src/http/service/openai.rs`),
and `DYN_LOG=debug` is mapped onto vLLM's and SGLang's Python loggers by
`dynamo.runtime.logging`. vLLM at DEBUG logs streamed completion text in its tool
parsers (`delta_text: %s`) and the rendered prompt on a placeholder mismatch
(`entrypoints/chat_utils.py`). `info` and above log neither.
"""


def is_verbose(level: str) -> bool:
    """Whether a log level, by name or by Python's number, is one that logs request content."""
    value = level.strip().lower()
    if value.isdigit():
        return int(value) < 20
    return value in VERBOSE_LEVELS


def _dyn_log_is_verbose(value: str) -> bool:
    """`DYN_LOG` is a list of tracing directives, `level` or `target=level`; any one verbose is enough."""
    return any(is_verbose(directive.rsplit("=", 1)[-1]) for directive in value.split(",") if directive.strip())


def _switched_on(value: str) -> bool:
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


CONTENT_LOGGING_ENV = {
    "DYN_LOG": _dyn_log_is_verbose,
    "DYN_LOGGING_CONFIG_PATH": lambda value: True,
    "VLLM_LOGGING_LEVEL": is_verbose,
    "VLLM_DEBUG_LOG_API_SERVER_RESPONSE": _switched_on,
}
"""Environment variables that can make a worker or the frontend log request content, and the values that do.

`DYN_LOGGING_CONFIG_PATH` is refused outright: it names a file of tracing filters
merged after `DYN_LOG`, which the plane cannot read. `VLLM_DEBUG_LOG_API_SERVER_RESPONSE`
logs every response body vLLM's own API server returns. A variable here set
through `valueFrom` is refused too, for the same reason as the config path. The
operator drops all of them again after the overlays are merged
(`withoutContentLoggingEnv` in `libs/operator/internal/render/retention.go`).
"""


def _refuse_flag(flag: str, where: str) -> None:
    raise Invalid(
        f"{where} may not set --{flag}: it writes request content to the worker's log, "
        "and request content is never retained",
        {"flag": flag},
    )


def _refuse_level(flag: str, level: str, where: str) -> None:
    raise Invalid(
        f"{where} may not set --{flag} to {level!r}: at that level the engine writes request content "
        "to the worker's log, and request content is never retained",
        {"flag": flag, "value": level},
    )


def check_retention(engine_args: dict | None, where: str = "the workload") -> None:
    """Refuses an `engine_args` map that turns on request-content logging.

    Both engines' parsers treat `_` and `-` in a flag name as the same character, so
    `enable_log_requests` is refused as well as `--enable-log-requests`.
    """
    for key, value in (engine_args or {}).items():
        flag = flag_name(str(key)).replace("_", "-")
        if flag in CONTENT_LOGGING_FLAGS:
            _refuse_flag(flag, where)
        if flag in LOG_LEVEL_FLAGS and is_verbose(str(value or "")):
            _refuse_level(flag, str(value), where)


def _check_tokens(tokens: Sequence[str]) -> None:
    """Reads a command line, `--flag value` and `--flag=value` alike, for a content-logging flag."""
    for index, token in enumerate(tokens):
        if not token.startswith("-"):
            continue
        name, has_value, value = token.partition("=")
        flag = flag_name(name).replace("_", "-")
        if flag in CONTENT_LOGGING_FLAGS:
            _refuse_flag(flag, "the overlay")
        if flag in LOG_LEVEL_FLAGS:
            level = value if has_value else (tokens[index + 1] if index + 1 < len(tokens) else "")
            if is_verbose(level):
                _refuse_level(flag, level, "the overlay")


def _check_env(entry: dict) -> None:
    name = entry.get("name")
    if not isinstance(name, str) or name not in CONTENT_LOGGING_ENV:
        return
    if "valueFrom" in entry:
        raise Invalid(
            f"the overlay may not set {name} from a reference: the plane cannot read its value, "
            "and some values of it write request content to the log",
            {"env": name},
        )
    value = entry.get("value")
    if value is not None and CONTENT_LOGGING_ENV[name](str(value)):
        raise Invalid(
            f"the overlay may not set {name}={value!r}: it writes request content to the log, "
            "and request content is never retained",
            {"env": name, "value": str(value)},
        )


def check_overlay_retention(overlay: object) -> None:
    """Refuses an overlay that turns on request-content logging on any container.

    The overlay is merged over the rendered pod and graph after the operator has
    assembled and filtered the engine's arguments, so a flag in a container's
    `args` or `command` there is the one spelling the operator cannot take back
    out. Every string in it is read as a possible flag, `--flag` and `--flag=value`
    alike, and the strings of one list are read as one command line so that
    `["--log-level", "debug"]` is seen as the pair it is. Every `{name, value}`
    map is read as a possible environment variable, which covers a pod's `env`
    and a Dynamo component's `envs` alike.
    """
    if isinstance(overlay, dict):
        _check_env(overlay)
        for value in overlay.values():
            check_overlay_retention(value)
        return
    if isinstance(overlay, list | tuple):
        _check_tokens([token for value in overlay if isinstance(value, str) for token in value.split()])
        for value in overlay:
            if not isinstance(value, str):
                check_overlay_retention(value)
        return
    if isinstance(overlay, str):
        _check_tokens(overlay.split())


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


def serving_digest(
    workload: Workload, artifact: Artifact, roles: Sequence[WorkloadRole], serving: dict | None = None
) -> str:
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

    `roles` is a parameter and not `workload.roles`, for the same reason `artifact` is: the
    dependency must be impossible to forget. Every relationship on this model is `lazy="noload"`,
    and a noloaded collection does not raise — SQLAlchemy hands back an **empty list**. A digest
    that read the attribute would therefore hash a disaggregated workload as if it had no roles
    whenever the caller had not asked for them, silently, and produce a digest that names a
    configuration nobody is running. Passing them in makes the omission a `TypeError` at the call
    site instead of a wrong hash in the database.

    `serving_topology` and the per-role configuration are added **only when the workload is
    disaggregated**, so every aggregated workload's digest is byte-identical to the one this
    function returned before roles existed. That is the same trick `engine_defaults` above plays
    and it is here for the same reason: an absent key and a default value are different hashes,
    and adding either unconditionally changes every digest in the fleet at migration — every
    deployment's `ServingDigestAnnotation` with it, and every benchmark's link back to what it
    measured.

    `router` is added **only when the workload declares a setting**, for the same reason. It is in
    the digest at all because it decides which worker serves a request and how long the request
    waits for one, and that is exactly what a benchmark run anchored on this digest measured. It
    is read with `getattr` because migrations hash rows from before `demand_workload.router`
    existed, and a row with no column declared no router.

    `serving` replaces the stored serving block in the hash when given. The workload
    service passes the block less the fields nothing renders for the workload's engine,
    so recording an intent that reaches no command line neither changes the digest nor
    restarts a worker. Left out, the stored block is hashed whole.
    """
    payload: dict[str, Any] = {"artifact": str(artifact.uid)}
    for key, attribute in DIGEST_FIELDS:
        value = serving if key == "serving" and serving is not None else getattr(workload, attribute)
        payload[key] = _canonical(value)
    # Only when the recipe actually supplies something, so that the digest of a model the
    # corpus does not cover is byte-identical to the one this function returned before
    # recipe defaults existed. An absent key and an empty map are different hashes, and
    # every workload in the fleet re-rendering once is the cost of confusing them.
    defaults = recipe_engine_args(workload, artifact)
    if defaults:
        payload["engine_defaults"] = _canonical(defaults)
    router = declared_router(getattr(workload, "router", None))
    if router:
        payload["router"] = router
    if workload.serving_topology == DISAGGREGATED:
        payload["serving_topology"] = workload.serving_topology
        payload["roles"] = [_role_config(role) for role in sorted(roles, key=lambda role: role.role)]
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return DIGEST_PREFIX + hashlib.sha256(body.encode("utf-8")).hexdigest()


ROUTER_FIELDS = ("mode", "queue_threshold", "queue_policy", "kv_overlap_score_credit")
"""The frontend router settings a workload can declare, in the order they render.

Each is one `DYN_ROUTER_*` variable on the deployment's own frontend. The operator renders them in
this order after `DYN_ROUTER_MODE`, and a field that is not declared renders nothing.
"""

ROUTER_NUMBERS = frozenset({"queue_threshold", "kv_overlap_score_credit"})


def declared_router(stored: dict | None) -> dict[str, Any]:
    """The router settings a workload declares, with every unset one left out.

    The one normalisation for a block with three readers: the column stores it, the digest hashes it
    and the wire sends it, and all three must agree that `{}` and a block of nulls are the same
    statement. If they did not, a workload that never declared a router would change its digest the
    first time anything rewrote its row, and every deployment of it would re-render.

    Numbers are coerced to `float` so that `1` and `1.0` hash the same: a JSON body may carry either,
    and JSONB hands back whichever was written.
    """
    declared: dict[str, Any] = {}
    for key in ROUTER_FIELDS:
        value = (stored or {}).get(key)
        if value is None:
            continue
        declared[key] = float(value) if key in ROUTER_NUMBERS else str(value)
    return declared


def _role_config(role: WorkloadRole) -> dict[str, Any]:
    """One role as the digest sees it: what it configures, never how many of it there are.

    `max_batch_tokens` and `engine_args` decide what the process is — prefill runs
    `--max-num-batched-tokens 32768` against decode's 2048, a sixteenfold difference in how much
    work one engine step does — so they belong in a hash whose subject is "what process runs".

    `weight` is deliberately absent, and the reason is identity rather than restarts.
    `ops_benchmark_run.serving_digest` names the configuration a measurement was taken against,
    and `DIGEST_FIELDS` is explicit that "a run whose digest no longer matches any workload has
    lost the only link back to what it measured". A weight in this hash would orphan every
    benchmark ever taken against a model the moment somebody added one prefill worker — and those
    benchmarks are precisely the measurements somebody would be consulting in order to choose the
    ratio. The rule is not new: `DIGEST_FIELDS` has always excluded scale, which is why the count
    of aggregated replicas is not in here either.

    The knock-on that has to be said out loud or the weights return through a side door:
    `device_count` *is* a digest field and it means **devices per worker** — 8 at TP8, not the
    group's 24. A group's device cost is `Σ (weight × device_count)`, derived where it is used
    and stored nowhere, because a column holding the group total would put the weights into this
    hash silently: nothing about the name says which of the two it holds.
    """
    return {
        "role": role.role,
        "max_batch_tokens": role.max_batch_tokens,
        "engine_args": _canonical(role.engine_args or {}),
    }


def _canonical(value: Any) -> Any:
    """A column value as something `json.dumps` will accept and always spell the same."""
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value
