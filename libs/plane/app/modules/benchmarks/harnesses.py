"""The load generators this plane knows how to point at a deployment.

One entry per harness, and an entry is small on purpose: the parameters it
accepts, their defaults, and the wire name the operator dispatches on. What a
harness *produces* is not described here at all — results are stored in whatever
shape the tool emits, and a schema for them would be this plane asserting it
knows the union of two output formats it has not read yet.

Parameters are declared rather than passed through blind because the console
renders a form from them and because a typo in a flag name should be a 400 here,
not a job that fails inside a cluster ten minutes later. `extra` is the escape
hatch for the long tail of flags nobody wants enumerated, and it is the caller's
problem if what they put in it is wrong.
"""

from typing import Any
from typing import NamedTuple

from app.core.exceptions import Invalid


class Parameter(NamedTuple):
    """One knob a harness accepts, and what it is when nobody sets it."""

    name: str
    type: type
    default: Any
    description: str


class Harness(NamedTuple):
    """A load generator, its knobs, and the image that carries it."""

    name: str
    wire: str
    image: str
    parameters: tuple[Parameter, ...]
    description: str


AIPERF = Harness(
    name="aiperf",
    wire="aiperf",
    image="nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0",
    description="NVIDIA AIPerf, the successor to GenAI-Perf. Closed-loop, concurrency-first.",
    parameters=(
        Parameter("concurrency", int, 10, "Requests in flight at once."),
        Parameter("request_count", int, 100, "Total requests to send."),
        Parameter("input_tokens_mean", int, 2000, "Mean synthetic prompt length, in tokens."),
        Parameter("input_tokens_stddev", int, 0, "Spread of the prompt length."),
        Parameter("output_tokens_mean", int, 256, "Mean generation length, in tokens."),
        Parameter("output_tokens_stddev", int, 0, "Spread of the generation length."),
        Parameter("endpoint_type", str, "chat", "AIPerf endpoint type: chat, completions, embeddings."),
        Parameter("streaming", bool, True, "Measure TTFT and ITL, which needs a streamed response."),
        Parameter("warmup_request_count", int, 10, "Requests sent and discarded before measuring."),
        Parameter("extra", str, "", "Extra flags, appended verbatim to the command line."),
    ),
)


HARNESSES: dict[str, Harness] = {AIPERF.name: AIPERF}
"""Every harness this plane can run.

vLLM's `benchmark_serving.py` and the built-in smoke check are the next two, and
they arrive as entries here plus a case in the operator's dispatcher. Nothing
else in this module is harness-specific, which is the point of the table.
"""

DEFAULT_HARNESS = AIPERF.name


def resolve(name: str) -> Harness:
    if name not in HARNESSES:
        raise Invalid(f"unknown benchmark harness {name!r}", {"allowed": sorted(HARNESSES)})
    return HARNESSES[name]


def normalise(harness: Harness, supplied: dict) -> dict:
    """Fills in the defaults and refuses anything the harness does not accept.

    Coercion is deliberate and narrow: a form posts `"10"` where an int is meant
    and rejecting that would make the console responsible for a distinction the
    HTTP layer already lost. A value that cannot be coerced is an error, because
    silently defaulting a bad number would produce a run that measured something
    other than what was asked for and say nothing about it.
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
        resolved[name] = _coerce(harness, parameter, supplied[name])
    return resolved


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


def wire_parameters(harness: Harness, parameters: dict, image: str | None = None) -> dict[str, str]:
    """The run, flattened to the string map `operation.desired` carries.

    Every value goes as a string because that is what the parameters map is; the
    operator parses them back against the same names. Booleans go as `true` and
    `false` rather than Python's capitalised repr, which `strconv.ParseBool` on
    the other side would not read.
    """
    flattened = {"harness": harness.name, "image": image or harness.image}
    for key, value in parameters.items():
        flattened[key] = "true" if value is True else "false" if value is False else str(value)
    return flattened
