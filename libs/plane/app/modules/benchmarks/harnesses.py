"""The harnesses this plane knows how to point at a serving surface.

One entry per harness, and an entry is small on purpose: the levels it makes
sense at, the parameters it accepts, their defaults, and the wire name the
operator dispatches on. What a harness *produces* is not described here at all —
results are stored in whatever shape the tool emits, and a schema for them would
be this plane asserting it knows the union of three output formats it has not
read yet. What one *means* is `summaries.py`, per version.

Parameters are declared rather than passed through blind because the console
renders a form from them and because a typo in a flag name should be a 400 here,
not a job that fails inside a cluster ten minutes later. `extra` is the escape
hatch for the long tail of flags nobody wants enumerated, and it is the caller's
problem if what they put in it is wrong.

The level is not a free choice per run. A harness that emits per-dataset scores
has nothing to say about one cluster's time-to-first-token, and a probe suite
has nothing to say about a deployment that has no public name — so `levels` is a
property of the harness and asking for one it does not declare is refused here,
the same way an unknown knob is.
"""

import json
import math

from typing import Any
from typing import NamedTuple

from app.core.exceptions import Invalid


DEPLOYMENT = "deployment"
WORKLOAD = "workload"
ENDPOINT = "endpoint"

LEVELS: tuple[str, ...] = (DEPLOYMENT, WORKLOAD, ENDPOINT)
"""The three things a run can be a measurement *of*, and they are different claims.

A deployment run measures one cluster's configuration — the device class, the
tensor-parallel degree, the quantization, the engine flags. A workload run
measures the serving configuration, which since ADR 0010 is a workload-level fact and is the
same experiment however many clusters it was placed on. An endpoint run measures
what a caller actually gets through the public name, across whatever the gateway
routed it to. Averaging any two of the three describes nothing that exists.
"""

DEFAULT_LEVEL = DEPLOYMENT


class Parameter(NamedTuple):
    """One knob a harness accepts, and what it is when nobody sets it.

    `levels` narrows a knob to a subset of the harness's own. Empty means every
    level the harness declares. A knob that only means something at one level —
    exercising a failover order, which needs a second target to fail over to —
    is refused elsewhere rather than quietly ignored.
    """

    name: str
    type: type
    default: Any
    description: str
    levels: tuple[str, ...] = ()


class Harness(NamedTuple):
    """A harness, the levels it grades at, its knobs, and the image that carries it."""

    name: str
    wire: str
    image: str
    levels: tuple[str, ...]
    parameters: tuple[Parameter, ...]
    description: str
    max_probes: int | None = None


DATASETS: tuple[str, ...] = (
    "aime2025",
    "aime2026",
    "hle",
    "mmlu_pro",
    "swe_bench_verified",
    "gpqa_diamond",
    "longbench_v2",
    "livecodebench",
    "tau2_bench",
)
"""The exams the acceptance standard names, spelled once.

Declared for the same reason a parameter is: a misspelled dataset is a run that
sits an exam nobody asked for, or none at all, and it should be a 400 here
rather than an empty `results` block an hour later. A dataset the standard does
not name is not refused because it is bad — it is refused because a score
against it has no declared baseline to be graded against.
"""


AIPERF = Harness(
    name="aiperf",
    wire="aiperf",
    image="nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0",
    levels=(DEPLOYMENT, WORKLOAD),
    description="NVIDIA AIPerf, the successor to GenAI-Perf. Closed-loop, concurrency-first.",
    parameters=(
        Parameter("concurrency", int, 10, "Requests in flight at once."),
        Parameter("request_count", int, 100, "Total requests to send."),
        Parameter("input_tokens_mean", int, 2000, "Mean synthetic prompt length, in tokens."),
        Parameter("input_tokens_stddev", int, 0, "Spread of the prompt length."),
        Parameter("output_tokens_mean", int, 256, "Mean generation length, in tokens."),
        Parameter("output_tokens_stddev", int, 0, "Spread of the generation length."),
        Parameter(
            "tokenizer",
            str,
            "",
            "HF tokenizer id used for client-side token counting; the model's own repository when empty.",
        ),
        Parameter("endpoint_type", str, "chat", "AIPerf endpoint type: chat, completions, embeddings."),
        Parameter("streaming", bool, True, "Measure TTFT and ITL, which needs a streamed response."),
        Parameter("warmup_request_count", int, 10, "Requests sent and discarded before measuring."),
        Parameter("extra", str, "", "Extra flags, appended verbatim to the command line."),
    ),
)


CONFORMANCE = Harness(
    name="conformance",
    wire="conformance",
    image="ghcr.io/hangar-dev/conformance:0.1.0",
    levels=(ENDPOINT, DEPLOYMENT),
    max_probes=200,
    description="Protocol, auth, error-shape and routing probes against a serving surface.",
    parameters=(
        Parameter(
            "suite",
            str,
            "all",
            "Probe group: chat, completions, embeddings, streaming, thinking, tools, "
            "structured, errors, auth, routing, all.",
        ),
        Parameter("strict", bool, True, "Treat advisory probes as required."),
        Parameter("request_timeout_s", int, 60, "Per-probe timeout."),
        Parameter("concurrency", int, 4, "Probes in flight; low by design — this is not a load test."),
        Parameter("max_output_tokens", int, 64, "Generation cap; probes assert shape, not content."),
        Parameter(
            "failover_probe",
            bool,
            False,
            "Exercise the endpoint's failover order.",
            levels=(ENDPOINT,),
        ),
        Parameter("extra", str, "", "Extra flags, appended verbatim to the command line."),
    ),
)
"""Not a load generator. A fixed, bounded catalogue of requests — most of them
deliberately malformed — asserting on the response *shape*: which protocols answer,
whether thinking mode and streaming behave as the workload declares, whether structured
output and tool calls come back in the documented envelope, and whether an error is the
error we promised. Its output is naturally boolean per probe.

`deployment` is a level because the same suite against a cluster-local address is how a
gateway fault is told from an engine fault, and that is the first question asked when an
endpoint probe fails. `failover_probe` is endpoint-only: below the endpoint there is
nothing to fail over to.

We own and pin the image. A probe suite is a statement of *our* contract, and vendoring
somebody else's opinion of what an OpenAI-compatible endpoint does and then grading
ourselves against it inverts the relationship. `max_probes` caps the enumerated failure
list, so it has a stated ceiling even in a total outage — the per-probe record itself is
telemetry and goes to object storage behind `artifacts_uri`, never onto the summary.
"""


EVALSCOPE = Harness(
    name="evalscope",
    wire="evalscope",
    image="ghcr.io/hangar-dev/evalscope:1.11.1",
    levels=(WORKLOAD, DEPLOYMENT, ENDPOINT),
    description="Accuracy and reasoning evaluation over an OpenAI-compatible surface.",
    parameters=(
        Parameter("datasets", str, "", "Comma-separated dataset ids; empty means the artifact's declared set."),
        Parameter("limit", int, 0, "Cap examples per dataset; 0 runs the full set."),
        Parameter("few_shot", int, 0, "Shots per example."),
        Parameter("temperature", float, 0.0, "Decoding temperature."),
        Parameter("top_p", float, 1.0, "Nucleus sampling cutoff."),
        Parameter("max_output_tokens", int, 32768, "Generation cap; reasoning datasets need headroom."),
        Parameter("seed", int, 0, "Sampling seed, recorded so that a rerun is a rerun."),
        Parameter("repeats", int, 1, "Passes per example, for pass@k style datasets."),
        Parameter("dataset_revision", str, "", "Pin of the dataset snapshot. Empty takes the image's pin."),
        Parameter("concurrency", int, 8, "Requests in flight."),
        Parameter("judge_base_url", str, "", "Grader's OpenAI-compatible base URL. Empty means no judge."),
        Parameter("judge_model", str, "", "Model name the grader serves."),
        Parameter("judge_credential_id", str, "", "Credential *reference* the executor redeems. Never a secret."),
        Parameter("judge_temperature", float, 0.0, "Decoding temperature for the grader."),
        Parameter("judge_max_output_tokens", int, 4096, "Generation cap for the grader."),
        Parameter("extra", str, "", "Extra flags, appended verbatim to the command line."),
    ),
)
"""Judged evaluation needs a model to grade, and this plane **has no opinion about which
model that is**. The judge is harness configuration and nothing more: a base URL, a model
name, a credential *reference*, and the two decoding knobs. Nothing here is pinned, no
default names a grader, and no code path anywhere in `libs/plane` holds credential
material — the reference is stored and the executor redeems it.

`dataset_revision`, `seed` and `repeats` are provenance wearing parameter clothes. A
dataset that gained or corrected items is a different exam; the reasoning sets on this
list have real run-to-run variance. A score that recorded none of the three cannot be
defended when somebody asks why the number moved.
"""


HARNESSES: dict[str, Harness] = {
    AIPERF.name: AIPERF,
    CONFORMANCE.name: CONFORMANCE,
    EVALSCOPE.name: EVALSCOPE,
}
"""Every harness this plane can run.

vLLM's `benchmark_serving.py` and `lm_eval` are the next two, and they arrive as
entries here plus a case in the operator's dispatcher. `lm_eval` arrives under its
own name and never as an alias of `evalscope`: two harnesses that grade the same
exam differently are two comparison groups, and `harness` plus `harness_version`
is what keeps them apart. Nothing else in this module is harness-specific, which is
the point of the table.
"""

DEFAULT_HARNESS = AIPERF.name


def resolve(name: str, level: str | None = None) -> Harness:
    """The named harness, refused when it does not grade at the level asked for."""
    if name not in HARNESSES:
        raise Invalid(f"unknown benchmark harness {name!r}", {"allowed": sorted(HARNESSES)})
    harness = HARNESSES[name]
    if level is not None:
        resolve_level(harness, level)
    return harness


def resolve_level(harness: Harness, level: str) -> str:
    """Refuses a level the harness has nothing to say at.

    A per-dataset accuracy score is not a statement about one cluster's latency
    and a probe against a public surface is not a statement about a deployment
    that has no public name. Both would run, both would produce a number, and
    the number would be attributed to a claim it does not support — which is the
    failure the provenance columns exist to prevent, one level up.
    """
    if level not in LEVELS:
        raise Invalid(f"unknown testing level {level!r}", {"allowed": list(LEVELS)})
    if level not in harness.levels:
        raise Invalid(
            f"{harness.name} does not grade at the {level} level",
            {"harness": harness.name, "level": level, "allowed": list(harness.levels)},
        )
    return level


def normalise(harness: Harness, supplied: dict, level: str | None = None) -> dict:
    """Fills in the defaults and refuses anything the harness does not accept.

    Coercion is deliberate and narrow: a form posts `"10"` where an int is meant
    and rejecting that would make the console responsible for a distinction the
    HTTP layer already lost. A value that cannot be coerced is an error, because
    silently defaulting a bad number would produce a run that measured something
    other than what was asked for and say nothing about it.

    A `level` also enforces the per-parameter narrowing: a knob restricted to
    another level is refused when it is set to anything but its default, rather
    than accepted and dropped somewhere the caller cannot see.
    """
    known = {parameter.name: parameter for parameter in harness.parameters}
    unknown = sorted(set(supplied) - set(known))
    if unknown:
        raise Invalid(
            f"{harness.name} does not accept {', '.join(unknown)}",
            {"harness": harness.name, "unknown": unknown, "allowed": sorted(known)},
        )

    resolved: dict = {}
    for name, parameter in known.items():
        if name not in supplied or supplied[name] is None:
            resolved[name] = parameter.default
            continue
        value = _coerce(harness, parameter, supplied[name])
        if level is not None and parameter.levels and level not in parameter.levels and value != parameter.default:
            raise Invalid(
                f"{harness.name} parameter {name!r} is {'/'.join(parameter.levels)} level only",
                {"harness": harness.name, "parameter": name, "level": level, "allowed": list(parameter.levels)},
            )
        resolved[name] = value

    if harness.name == EVALSCOPE.name:
        resolved["datasets"] = ",".join(resolve_datasets(resolved.get("datasets") or ""))
        check_judge(harness, resolved, level)
    return resolved


def resolve_datasets(supplied: str) -> tuple[str, ...]:
    """The exams a run will sit, refusing one nothing declares a baseline for.

    Empty is not an error and is not a default set: it means the artifact's own
    declared baselines say which exams apply, which is resolved against the
    baseline table at expansion rather than guessed at here.
    """
    named = [entry.strip().lower() for entry in supplied.split(",") if entry.strip()]
    unknown = sorted({entry for entry in named if entry not in DATASETS})
    if unknown:
        raise Invalid(
            f"unknown dataset {', '.join(unknown)}",
            {"unknown": unknown, "allowed": list(DATASETS)},
        )
    seen: list[str] = []
    for entry in named:
        if entry not in seen:
            seen.append(entry)
    return tuple(seen)


def check_judge(harness: Harness, parameters: dict, level: str | None) -> None:
    """Refuses a judge this plane cannot honour at the level asked for.

    A workload- or deployment-level run is dispatched into a cluster, into a pod
    built for no egress and mounting no credential material. A judge it can
    resolve locally is fine and this plane cannot verify that either way — what
    it can see is a `judge_credential_id`, whose presence is the tell that the
    grader is somewhere the cluster was not built to reach. So that is what is
    refused, and a judged run with an external grader is endpoint-level work.

    A base URL without a model name is refused everywhere: it names a grader
    nothing can call, and a run that silently graded nothing would report a
    score that means the opposite of what it says.
    """
    base_url = str(parameters.get("judge_base_url") or "").strip()
    model = str(parameters.get("judge_model") or "").strip()
    credential = str(parameters.get("judge_credential_id") or "").strip()

    if bool(base_url) != bool(model):
        raise Invalid(
            "a judge needs both a base URL and a model name",
            {"harness": harness.name, "judge_base_url": base_url, "judge_model": model},
        )
    if credential and not base_url:
        raise Invalid(
            "a judge credential names a grader that no base URL points at",
            {"harness": harness.name},
        )
    if credential and level in (DEPLOYMENT, WORKLOAD):
        raise Invalid(
            f"a {level} level run is executed inside a cluster and cannot redeem a judge credential",
            {"harness": harness.name, "level": level, "allowed": [ENDPOINT]},
        )


def _coerce(harness: Harness, parameter: Parameter, value: Any) -> Any:
    try:
        if parameter.type is bool:
            if isinstance(value, bool):
                return value
            lowered = str(value).strip().lower()
            if lowered in {"true", "1", "yes"}:
                return True
            if lowered in {"false", "0", "no"}:
                return False
            raise ValueError(value)
        if parameter.type is int:
            coerced = int(value)
            if coerced < 0:
                raise ValueError(value)
            return coerced
        if parameter.type is float:
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError(value)
            return number
        return str(value)
    except (TypeError, ValueError) as exc:
        raise Invalid(
            f"{harness.name} parameter {parameter.name!r} is not a {parameter.type.__name__}",
            {"harness": harness.name, "parameter": parameter.name, "value": value},
        ) from exc


def resolve_image(harness: Harness, supplied: str | None) -> str:
    """The image a run will actually pull, which is the harness default unless asked otherwise.

    The default here is a pin, not a floating tag, so a table of runs stays
    comparable and a harness release cannot change what a rerun measures. An
    override exists because that pin will always trail the registry, and waiting
    for a plane deploy to try a newer AIPerf is a poor reason not to measure.

    Only the reference's shape is checked. Whether the image exists is the
    registry's answer and the plane cannot have it; a wrong one surfaces as a
    pull failure on the run, which is where a wrong image belongs.
    """
    if supplied is None:
        return harness.image
    image = supplied.strip()
    if not image:
        return harness.image
    if len(image) > 512 or any(c.isspace() for c in image):
        raise Invalid("benchmark image is not a valid image reference", {"image": supplied})
    return image


def wire_parameters(
    harness: Harness,
    parameters: dict,
    image: str | None = None,
    model: str | None = None,
    endpoint_model: str | None = None,
) -> dict[str, str]:
    """The run, flattened to the string map `operation.desired` carries.

    Every value goes as a string because that is what the parameters map is; the
    operator parses them back against the same names. Booleans go as `true` and
    `false` rather than Python's capitalised repr, which `strconv.ParseBool` on
    the other side would not read.

    `model` is the served model name the run asks for, resolved from the deployment's
    workload and artifact. It is not a harness parameter — the harness has no default
    for it because there is no default model — but the operator keys everything it
    renders off it (`parameters["model"]` at the top of `BenchmarkJob`), so it travels
    inside the same map rather than beside it.

    `endpoint_model` is what the harness should put in the request body's `model`
    field when it dials the endpoint. That is almost always the same string as
    `model`, but not always: a deployment named `gpt-oss-20b` may be served by a
    frontend that only answers under the artifact's HF repo id `openai/gpt-oss-20b`.
    EvalScope is the harness that cares, because its OpenAI client is strict about
    the field; AIPerf builds its own requests and tolerates the served name.
    """
    flattened = {"harness": harness.name, "image": image or harness.image}
    if model:
        flattened["model"] = model
    for key, value in parameters.items():
        flattened[key] = "true" if value is True else "false" if value is False else str(value)
    if harness.name == EVALSCOPE.name:
        flattened["command"] = _evalscope_command(parameters, endpoint_model or model or "")
        flattened["version_command"] = "evalscope --version"
    return flattened


def _evalscope_command(parameters: dict, model: str) -> str:
    """The shell line the operator's generic case runs for an evalscope run.

    Lives here rather than in the operator because evalscope's CLI is a property of the harness
    the plane pinned, not of the thing that schedules it. A new evalscope release changes this
    function and nothing in `render/benchmark.go`. The operator renders the line verbatim — no
    re-quoting, no flag assembly — which is what keeps the generic case generic.

    Every value is shell-quoted on the way in, because every one is attacker-adjacent by the
    time it reaches a pod: a dataset id, a base URL and a model name all originate as strings
    somebody typed. `--limit 0` and `repeats 1` are still emitted rather than omitted, because
    they are the harness's own defaults and naming them makes the command line the record of
    what was asked for.
    """
    args: list[str] = ["evalscope", "eval"]
    args += ["--model", _shell(model)]
    # The endpoint under test is a cluster-local fact the plane does not hold: the operator's
    # renderer exports it as $HANGAR_BENCHMARK_ENDPOINT before it runs this line, exactly the way
    # aiperfArgs receives it as an argument. The judge's base URL is different — it is harness
    # configuration, and it travels in the parameters like everything else.
    args += ["--api-url", '"$HANGAR_BENCHMARK_ENDPOINT"']
    # EvalScope against a remote OpenAI-compatible surface is `openai_api`; setting this
    # explicitly keeps the auto-inference from having a say on a URL shape it was not built
    # around.
    args += ["--eval-type", "openai_api"]
    # No timestamp dir: the operator's wrapper finds the run's output by name, and a
    # timestamped subdir makes the newest the only one that matters.
    args += ["--no-timestamp"]
    args += ["--work-dir", "/benchmark-artifacts"]

    datasets = str(parameters.get("datasets") or "").strip()
    if datasets:
        # EvalScope's `--datasets` is a space-separated list, not a comma-joined string.
        args += ["--datasets"] + [_shell(name) for name in datasets.split(",") if name.strip()]
    limit = int(parameters.get("limit") or 0)
    if limit > 0:
        args += ["--limit", str(limit)]
    args += ["--seed", str(int(parameters.get("seed") or 0))]
    repeats = int(parameters.get("repeats") or 1)
    if repeats != 1:
        args += ["--repeats", str(repeats)]

    # Decoding and shots are harness-side names for what EvalScope keeps inside two JSON
    # bags: `--generation-config` for sampling and `--dataset-args` for per-dataset knobs
    # like `few_shot`. Concurrency and dataset revision are not CLI flags at 1.11.1;
    # concurrency lives at the batch level and revision is a dataset-args property.
    generation_config = {
        "temperature": float(parameters.get("temperature") or 0.0),
        "top_p": float(parameters.get("top_p") or 1.0),
        "max_tokens": int(parameters.get("max_output_tokens") or 32768),
    }
    args += ["--generation-config", _shell(json.dumps(generation_config, separators=(",", ":")))]
    dataset_args: dict[str, dict] = {}
    few_shot = int(parameters.get("few_shot") or 0)
    revision = str(parameters.get("dataset_revision") or "").strip()
    if few_shot or revision:
        for name in (d for d in datasets.split(",") if d.strip()):
            entry: dict = {}
            if few_shot:
                entry["few_shot_num"] = few_shot
            if revision:
                entry["revision"] = revision
            dataset_args[name] = entry
        args += ["--dataset-args", _shell(json.dumps(dataset_args, separators=(",", ":")))]

    # The judge is one typed JSON object at 1.11.1, not a spread of flags. `judge_credential_id`
    # is redeemed at dispatch time by the endpoint-level executor; at deployment and workload
    # levels the field never arrives here (`check_judge` refuses it), and a judge the cluster
    # reaches authenticates by being cluster-local.
    base_url = str(parameters.get("judge_base_url") or "").strip()
    judge_model = str(parameters.get("judge_model") or "").strip()
    if base_url and judge_model:
        judge = {
            "base_url": base_url,
            "model": judge_model,
            "temperature": float(parameters.get("judge_temperature") or 0.0),
            "max_tokens": int(parameters.get("judge_max_output_tokens") or 4096),
        }
        args += ["--judge", _shell(json.dumps(judge, separators=(",", ":")))]

    extra = str(parameters.get("extra") or "").strip()
    if extra:
        args.append(extra)
    return " ".join(args)


def _shell(value: str) -> str:
    """Single-quote a value for the shell the operator will run it under."""
    return "'" + value.replace("'", "'\\''") + "'"
