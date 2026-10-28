"""The harnesses this plane knows how to point at a serving surface.

One entry per harness, and an entry is small by design: the levels it makes
sense at, the parameters it accepts, their defaults, and the wire name the
operator dispatches on. What a harness *produces* is not described here at all:
results are stored in whatever shape the tool emits, and a schema for them would
be this plane asserting it knows the union of three output formats it has not
read yet. What one *means* is `summaries.py`, per version.

Parameters are declared, not passed through blind because the console
renders a form from them and because a typo in a flag name should be a 400 here,
not a job that fails inside a cluster ten minutes later. There is no free-form
flag or command-line parameter: the operator builds each harness's argv itself
from these typed values and refuses a run that carries text for a shell, so a
knob that is not declared here is not one a run can turn.

The level is not a free choice per run. A harness that emits per-dataset scores
has nothing to say about one cluster's time-to-first-token, and a probe suite
has nothing to say about a deployment that has no public name, so `levels` is a
property of the harness and asking for one it does not declare is refused here,
the same way an unknown knob is.
"""

import math

from typing import Any
from typing import NamedTuple

from app.config import settings
from app.core.exceptions import Invalid


DEPLOYMENT = "deployment"
WORKLOAD = "workload"
ENDPOINT = "endpoint"

LEVELS: tuple[str, ...] = (DEPLOYMENT, WORKLOAD, ENDPOINT)
"""The three things a run can be a measurement *of*, and they are different claims.

A deployment run measures one cluster's configuration: the device class, the
tensor-parallel degree, the quantization, the engine flags. A workload run
measures the serving configuration, which since ADR 0010 is a workload-level fact and is the
same experiment however many clusters it was placed on. An endpoint run measures
what a caller actually gets through the public name, across whatever the gateway
routed it to. Averaging any two of the three describes nothing that exists.
"""

RUNNABLE_LEVELS: tuple[str, ...] = (DEPLOYMENT, WORKLOAD)
"""The levels something executes today.

`endpoint` stays declared on the harnesses that grade there, because that is a
fact about the harness. Nothing runs it yet: a cluster's benchmark job never
dials out to a public name, and the executor for the public surface is roadmap.
The catalogue serves only these levels, and only the knobs that mean something
at one of them, so the console never offers what `create_request` refuses.
"""

DEFAULT_LEVEL = DEPLOYMENT


def runnable(levels: tuple[str, ...]) -> list[str]:
    """The subset of `levels` something executes, in the order they were declared."""
    return [level for level in levels if level in RUNNABLE_LEVELS]


class Parameter(NamedTuple):
    """One knob a harness accepts, and what it is when nobody sets it.

    `levels` narrows a knob to a subset of the harness's own. Empty means every
    level the harness declares. A knob that only means something at one level
    (exercising a failover order, which needs a second target to fail over to)
    is refused elsewhere, not quietly ignored.
    """

    name: str
    type: type
    default: Any
    description: str
    levels: tuple[str, ...] = ()


class Harness(NamedTuple):
    """A harness, the levels it grades at, its knobs, and the image that carries it.

    The image is either pinned here (`pinned_image`, for a public upstream image) or read from
    the plane setting named by `image_setting`, for images an operator builds and hosts.
    """

    name: str
    wire: str
    levels: tuple[str, ...]
    parameters: tuple[Parameter, ...]
    description: str
    max_probes: int | None = None
    pinned_image: str = ""
    image_setting: str = ""

    @property
    def image(self) -> str:
        """The default image for a run, or empty when its setting is unset."""
        if self.image_setting:
            return str(getattr(settings, self.image_setting) or "").strip()
        return self.pinned_image


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
instead of an empty `results` block an hour later. A dataset the standard does
not name is not refused because it is bad: it is refused because a score
against it has no declared baseline to be graded against.
"""


AIPERF = Harness(
    name="aiperf",
    wire="aiperf",
    pinned_image="nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0",
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
    ),
)


CONFORMANCE = Harness(
    name="conformance",
    wire="conformance",
    image_setting="BENCHMARK_CONFORMANCE_IMAGE",
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
        Parameter("concurrency", int, 4, "Probes in flight; low by design: this is not a load test."),
        Parameter("max_output_tokens", int, 64, "Generation cap; probes assert shape, not content."),
        Parameter(
            "failover_probe",
            bool,
            False,
            "Exercise the endpoint's failover order.",
            levels=(ENDPOINT,),
        ),
    ),
)
"""Not a load generator. A fixed, bounded catalogue of requests, most of them
intentionally malformed, asserting on the response *shape*: which protocols answer,
whether thinking mode and streaming behave as the workload declares, whether structured
output and tool calls come back in the documented envelope, and whether an error is the
error we promised. Its output is naturally boolean per probe.

`deployment` is a level because the same suite against a cluster-local address is how a
gateway fault is told from an engine fault, and that is the first question asked when an
endpoint probe fails. `failover_probe` is endpoint-only: below the endpoint there is
nothing to fail over to.

The image is named by `WINGSMITH_BENCHMARK_CONFORMANCE_IMAGE` or the request's `image`. The plane
composes no command line for this harness yet, so the operator refuses to render a run until it does.
`libs/conformance` builds `wingsconform`, as a CLI or an image, which runs probes of this kind from a
workstation today but is not what this harness drives.
`max_probes` caps the enumerated failure list, so it has a stated ceiling even in a total outage;
the per-probe record goes to object storage behind `artifacts_uri`, never onto the summary.
"""


EVALSCOPE = Harness(
    name="evalscope",
    wire="evalscope",
    image_setting="BENCHMARK_EVALSCOPE_IMAGE",
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
        Parameter(
            "judge_credential_id",
            str,
            "",
            "Credential *reference* the executor redeems. Never a secret.",
            levels=(ENDPOINT,),
        ),
        Parameter("judge_temperature", float, 0.0, "Decoding temperature for the grader."),
        Parameter("judge_max_output_tokens", int, 4096, "Generation cap for the grader."),
    ),
)
"""Judged evaluation needs a model to grade, and this plane **has no opinion about which
model that is**. The judge is harness configuration and nothing more: a base URL, a model
name, a credential *reference*, and the two decoding knobs. Nothing here is pinned, no
default names a grader, and no code path anywhere in `libs/plane` holds credential
material: the reference is stored and the executor redeems it.

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

A new harness is an entry here plus a case in the operator's dispatcher. Two harnesses that grade
the same exam differently are separate entries, never aliases: `harness` plus `harness_version`
is what keeps their results apart.
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
    the number would be attributed to a claim it does not support, which is the
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
            allowed = runnable(parameter.levels)
            message = f"{harness.name} parameter {name!r} is {'/'.join(parameter.levels)} level only"
            if not allowed:
                message += ", and nothing runs at that level yet"
            raise Invalid(
                message,
                {"harness": harness.name, "parameter": name, "level": level, "allowed": allowed},
            )
        resolved[name] = value

    if harness.name == EVALSCOPE.name:
        resolved["datasets"] = ",".join(resolve_datasets(resolved.get("datasets") or ""))
        check_judge(harness, resolved)
    return resolved


def resolve_datasets(supplied: str) -> tuple[str, ...]:
    """The exams a run will sit, refusing one nothing declares a baseline for.

    Empty is not an error and is not a default set: it means the artifact's own
    declared baselines say which exams apply, which is resolved against the
    baseline table at expansion, not guessed at here.
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


def check_judge(harness: Harness, parameters: dict) -> None:
    """Refuses a judge nothing can call.

    A workload- or deployment-level run is dispatched into a cluster, into a pod
    built for no egress and mounting no credential material. A judge it can
    resolve locally is fine and this plane cannot verify that either way: what
    it can see is a `judge_credential_id`, whose presence is the tell that the
    grader is somewhere the cluster was not built to reach. That parameter is
    therefore endpoint-level, and `normalise` refuses it below the endpoint
    before this runs; a judged run with an external grader waits on the
    endpoint executor.

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
    """The image a run will pull: the caller's, else the harness default.

    Only the reference's shape is checked; a missing image surfaces as a pull failure on the run.
    An override is still bounded by the cluster: the operator renders only images listed in its
    `WINGSMITH_BENCHMARK_IMAGES` and fails any other run, so naming one here does not put it there.
    A harness whose image setting is unset is refused unless the caller names an image.
    """
    image = (supplied or "").strip()
    if not image:
        image = harness.image
        if not image:
            variable = f"WINGSMITH_{harness.image_setting}"
            raise Invalid(
                f"the {harness.name} harness has no image configured; set {variable} on the plane"
                " or pass `image` with the request",
                {"harness": harness.name, "setting": variable},
            )
        return image
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
    `false`, not Python's capitalised repr, which `strconv.ParseBool` on
    the other side would not read.

    `model` is the served model name the run asks for, resolved from the deployment's
    workload and artifact. It is not a harness parameter (the harness has no default
    for it because there is no default model), but the operator keys everything it
    renders off it (`parameters["model"]` at the top of `BenchmarkJob`), so it travels
    inside the same map, not beside it.

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
    if harness.name == EVALSCOPE.name and endpoint_model and endpoint_model != model:
        flattened["endpoint_model"] = endpoint_model
    return flattened
