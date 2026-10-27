"""A suite plan: which models, which harnesses, at which load points, how often.

A plan is TOML because a benchmark plan is a document somebody edits and commits,
and because `tomllib` is in the standard library: this package has no
dependencies, which is what lets it run from any checkout without a virtualenv
of its own.

Expansion is intentionally dumb: the cartesian product of models × harnesses ×
load points × repeats, in a fixed order, with the repeat index part of the cell
id. Nothing here is clever about scheduling, because there is nothing to be
clever about: the cells run one at a time (see `runner`), since two load
generators pointed at one deployment measure each other.
"""

from __future__ import annotations

import tomllib

from typing import Any
from pathlib import Path
from dataclasses import field
from dataclasses import dataclass


DEFAULT_TOKEN_ENV = "HANGAR_API_KEY"
ENDPOINT_KEYS = frozenset({"url", "probe_url", "token_env"})


@dataclass(frozen=True)
class Endpoint:
    """The serving surface under test, from both sides of the container boundary.

    `url` is resolved inside the container, `probe_url` from the host. They differ
    whenever the endpoint is a port-forward on this machine: a container reaches it
    at `host.docker.internal`, and the host cannot resolve that name at all.

    `token_env` names the environment variable holding the bearer token, read once
    when the run starts. Against the gateway's public listener that is a tenant API
    key; its internal listener takes none, and the variable can stay unset.
    """

    url: str
    probe_url: str
    token_env: str = DEFAULT_TOKEN_ENV


@dataclass(frozen=True)
class Model:
    """One model to measure, and the tokenizer that counts its tokens.

    The tokenizer is a Hugging Face repository and not the served name. Every
    harness here counts tokens client-side; pointed at the served name, each of
    them fails to load a tokenizer before it sends a request.
    """

    name: str
    tokenizer: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Load:
    """One point on the load curve.

    `concurrency` is requests in flight. `request_rate` is arrivals per second for
    the open-loop harnesses, where `0` means closed-loop at `concurrency`. A point
    carries either a request budget or a time budget; a harness that only
    understands the other one converts, and records what it actually did.
    """

    concurrency: int
    requests: int = 0
    seconds: int = 0
    request_rate: float = 0.0

    @property
    def label(self) -> str:
        if self.request_rate:
            return f"{self.request_rate:g} req/s"
        return f"c{self.concurrency}"


@dataclass(frozen=True)
class Sweep:
    """A whole load ladder driven by one harness process, not by this runner.

    Two of the harnesses can do this and it is worth using: one tokenizer load, one
    continuous client, and the endpoint sees one session walking up the ladder
    instead of N cold starts. `vllm-sweep` also produces upstream's own summary and
    plot artifacts, which is the reason the tool exists.

    `num_runs` is repeats *per rung*, inside the harness, which is not the same as
    the plan's `repeats`: the harness averages them and reports one summary.
    """

    enabled: bool = False
    harnesses: tuple[str, ...] = ("vllm-sweep",)
    ladder: tuple[int, ...] = (1, 2, 4, 8)
    requests_per_rung: int = 20
    seconds_per_rung: int = 0
    num_runs: int = 1


@dataclass(frozen=True)
class SweepSpec:
    """One sweep: one model, one ladder, one harness.

    Carried as its own object because a sweep is not a cell: it is a request for a
    whole curve, and the measurements come back labelled by what the harness
    actually ran, not by what was asked for.
    """

    harness: str
    model: Model
    ladder: tuple[int, ...]
    input_tokens: int
    output_tokens: int
    requests_per_rung: int
    seconds_per_rung: int
    num_runs: int = 1

    @property
    def id(self) -> str:
        rungs = "-".join(str(rung) for rung in self.ladder)
        return f"{self.model.name}--{self.harness}--ladder{rungs}--isl{self.input_tokens}-osl{self.output_tokens}"


@dataclass(frozen=True)
class Knee:
    """The saturation search: raise load until the endpoint stops meeting an SLA.

    This is the one stage that is allowed to hurt a live endpoint, so it is opt-in
    per plan and again per invocation. `field` is a canonical metric id from
    `metrics`, `limit` is in that field's unit, and `ladder` is climbed in order:
    the search stops at the first rung that breaks the SLA or errors, and reports
    the last rung that held.
    """

    enabled: bool = False
    harness: str = "aiperf"
    field: str = "ttft.p90"
    limit: float = 2000.0
    error_limit: int = 0
    ladder: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64)
    requests_per_rung: int = 0
    seconds_per_rung: int = 0


@dataclass(frozen=True)
class Plan:
    """A whole suite, as committed to the repository."""

    name: str
    description: str
    endpoint: Endpoint
    models: tuple[Model, ...]
    harnesses: tuple[str, ...]
    load: tuple[Load, ...]
    repeats: int = 1
    cooldown_seconds: int = 10
    input_tokens: int = 2000
    output_tokens: int = 256
    sweep: Sweep = field(default_factory=Sweep)
    knee: Knee = field(default_factory=Knee)
    source: str = ""

    def tokens_for(self, model: Model) -> tuple[int, int]:
        """The prompt and generation lengths for one model, plan defaults filled in."""
        return (
            model.input_tokens or self.input_tokens,
            model.output_tokens or self.output_tokens,
        )


@dataclass(frozen=True)
class Cell:
    """One measurement: one harness, one model, one load point, one repeat.

    The cell id is the directory name its artifacts land in and the key the report
    joins on, so it has to be stable across runs of the same plan: that is what
    makes two suite runs comparable, not merely adjacent.
    """

    harness: str
    model: Model
    load: Load
    repeat: int
    input_tokens: int
    output_tokens: int
    stage: str = "matrix"

    @property
    def id(self) -> str:
        rate = f"-r{self.request_rate_slug}" if self.load.request_rate else ""
        return (
            f"{self.model.name}--{self.harness}--c{self.load.concurrency}{rate}"
            f"--isl{self.input_tokens}-osl{self.output_tokens}--{self.repeat}"
        )

    @property
    def request_rate_slug(self) -> str:
        return f"{self.load.request_rate:g}".replace(".", "_")


def _endpoint(raw: dict[str, Any]) -> Endpoint:
    unknown = sorted(set(raw) - ENDPOINT_KEYS)
    if unknown:
        raise ValueError(
            f"[endpoint] has no {', '.join(unknown)}; it takes {', '.join(sorted(ENDPOINT_KEYS))}, "
            "and the token is read from the variable token_env names"
        )
    url = raw.get("url", "http://host.docker.internal:8080")
    return Endpoint(
        url=url,
        probe_url=raw.get("probe_url", url.replace("host.docker.internal", "localhost")),
        token_env=raw.get("token_env", DEFAULT_TOKEN_ENV),
    )


def _model(raw: dict[str, Any]) -> Model:
    name = raw["name"]
    return Model(
        name=name,
        tokenizer=raw.get("tokenizer", ""),
        input_tokens=int(raw.get("input_tokens", 0)),
        output_tokens=int(raw.get("output_tokens", 0)),
    )


def _load(raw: dict[str, Any]) -> Load:
    return Load(
        concurrency=int(raw.get("concurrency", 1)),
        requests=int(raw.get("requests", 0)),
        seconds=int(raw.get("seconds", 0)),
        request_rate=float(raw.get("request_rate", 0.0)),
    )


def _sweep(raw: dict[str, Any]) -> Sweep:
    return Sweep(
        enabled=bool(raw.get("enabled", False)),
        harnesses=tuple(raw.get("harnesses", ("vllm-sweep",))),
        ladder=tuple(int(rung) for rung in raw.get("ladder", (1, 2, 4, 8))),
        requests_per_rung=int(raw.get("requests_per_rung", 20)),
        seconds_per_rung=int(raw.get("seconds_per_rung", 0)),
        num_runs=int(raw.get("num_runs", 1)),
    )


def _knee(raw: dict[str, Any]) -> Knee:
    ladder = tuple(int(rung) for rung in raw.get("ladder", (1, 2, 4, 8, 16, 32, 64)))
    return Knee(
        enabled=bool(raw.get("enabled", False)),
        harness=raw.get("harness", "aiperf"),
        field=raw.get("field", "ttft.p90"),
        limit=float(raw.get("limit", 2000.0)),
        error_limit=int(raw.get("error_limit", 0)),
        ladder=ladder,
        requests_per_rung=int(raw.get("requests_per_rung", 0)),
        seconds_per_rung=int(raw.get("seconds_per_rung", 0)),
    )


def load_plan(path: Path) -> Plan:
    """Read a plan file, with every default filled in."""
    raw = tomllib.loads(path.read_text())
    models = tuple(_model(entry) for entry in raw.get("models", ()))
    if not models:
        raise ValueError(f"{path}: a plan with no models measures nothing")
    load = tuple(_load(entry) for entry in raw.get("load", ()))
    if not load:
        raise ValueError(f"{path}: a plan with no load points measures nothing")
    harnesses = tuple(raw.get("harnesses", ("aiperf",)))
    if not harnesses:
        raise ValueError(f"{path}: a plan with no harnesses measures nothing")
    return Plan(
        name=raw.get("name", path.stem),
        description=raw.get("description", ""),
        endpoint=_endpoint(raw.get("endpoint", {})),
        models=models,
        harnesses=harnesses,
        load=load,
        repeats=int(raw.get("repeats", 1)),
        cooldown_seconds=int(raw.get("cooldown_seconds", 10)),
        input_tokens=int(raw.get("input_tokens", 2000)),
        output_tokens=int(raw.get("output_tokens", 256)),
        sweep=_sweep(raw.get("sweep", {})),
        knee=_knee(raw.get("knee", {})),
        source=str(path),
    )


def sweep_specs(plan: Plan) -> list[SweepSpec]:
    """Every ladder-in-one-process run the plan asks for."""
    specs: list[SweepSpec] = []
    if not plan.sweep.enabled:
        return specs
    for model in plan.models:
        isl, osl = plan.tokens_for(model)
        for harness in plan.sweep.harnesses:
            specs.append(
                SweepSpec(
                    harness=harness,
                    model=model,
                    ladder=plan.sweep.ladder,
                    input_tokens=isl,
                    output_tokens=osl,
                    requests_per_rung=plan.sweep.requests_per_rung,
                    seconds_per_rung=plan.sweep.seconds_per_rung,
                    num_runs=plan.sweep.num_runs,
                )
            )
    return specs


def expand(plan: Plan) -> list[Cell]:
    """The plan as a flat list of measurements, in the order they will run.

    Model outermost, then load point, then harness, then repeat. The order is the
    one that keeps a comparison fair: the harnesses that share a load point run
    back to back, so a drift in the endpoint's own condition lands on all of them,
    not on whichever one ran last.
    """
    cells: list[Cell] = []
    for model in plan.models:
        isl, osl = plan.tokens_for(model)
        for load in plan.load:
            for harness in plan.harnesses:
                for repeat in range(1, plan.repeats + 1):
                    cells.append(
                        Cell(
                            harness=harness,
                            model=model,
                            load=load,
                            repeat=repeat,
                            input_tokens=isl,
                            output_tokens=osl,
                        )
                    )
    return cells


def knee_cells(plan: Plan, model: Model) -> list[Cell]:
    """The saturation ladder for one model, as cells the runner can execute.

    Returned whole instead of generated lazily, so that `--dry-run` can show the
    ceiling of what the search might do. The runner stops climbing early, and the
    cells it never ran are simply absent from the results.
    """
    isl, osl = plan.tokens_for(model)
    cells: list[Cell] = []
    for rung in plan.knee.ladder:
        load = Load(
            concurrency=rung,
            requests=plan.knee.requests_per_rung,
            seconds=plan.knee.seconds_per_rung,
        )
        cells.append(
            Cell(
                harness=plan.knee.harness,
                model=model,
                load=load,
                repeat=1,
                input_tokens=isl,
                output_tokens=osl,
                stage="knee",
            )
        )
    return cells
