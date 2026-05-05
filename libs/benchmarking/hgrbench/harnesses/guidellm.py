"""GuideLLM, the one harness that measures more than one load point per process.

Its whole shape is the *profile*: one run walks a sequence of load points and puts
them all in one result file. That is why a cell here can produce several
measurements, and why `read` returns a list.

Two profiles matter to us and the difference is a safety question:

- `kind=concurrent` with `--override 'profile.streams' 1,2,4,8` runs one benchmark
  per rung of a ladder we chose. Bounded, predictable, safe against a live endpoint.
- `kind=sweep,sweep_size=N` self-calibrates instead: benchmark 1 is serial,
  benchmark 2 is `throughput` — deliberately unbounded, `max_concurrency` defaulting
  to 512 — and the remaining rates are `linspace` between the two rates *this*
  server actually achieved. It adapts per model, which is genuinely better data, and
  its second leg is an overload by construction. It is behind `--hard` for that
  reason, and pinned to a `max_concurrency` even then.

The CLI was rewritten in 0.7. `guidellm benchmark` does not exist, and neither do
`--target`, `--rate-type`, `--rate`, `--max-seconds` or `--max-requests`; every
setting is a composite `--<group> kind=<name>,key=value`. Anything written against
the older surface fails outright, which is at least a loud failure.

Three traps are handled here rather than discovered again:

- `validate_backend` defaults to true and issues `GET /health` with
  `raise_for_status`. Our gateway answers 400 there with a token and 401 without,
  so the default aborts every run with a message about not being able to connect —
  which reads like a network fault and is not one.
- `--constraint kind=max_requests,count=N` is **per benchmark**, not per run. A
  four-rung ladder with `count=20` sends 80 requests.
- There is no header flag and no environment variable for the key. `api_key=` is
  the only route, so the run goes through a shell inside the container and the
  token is expanded there — never in our argv.
"""

from __future__ import annotations

from typing import Any

from hgrbench.plan import Cell
from hgrbench.plan import SweepSpec
from hgrbench.harnesses.base import TOKEN_ENV
from hgrbench.harnesses.base import Context
from hgrbench.harnesses.base import Harness
from hgrbench.harnesses.base import Emission
from hgrbench.harnesses.base import Invocation
from hgrbench.harnesses.base import read_json
from hgrbench.harnesses.base import shell_command


IMAGE = "ghcr.io/vllm-project/guidellm:latest"
PINNED_VERSION = "0.7.3"
RESULTS = "/results"
EXPORT = "benchmarks.json"


def command(cell: Cell, ctx: Context) -> Invocation:
    """One GuideLLM run at one operating point.

    A single point rather than a ladder, so that this harness sits in the same
    matrix as the other two and the comparison at a given concurrency is a
    comparison. The ladder-in-one-process mode is what `sweep_command` is for.

    `--metrics kind=generative,sample_size=50` caps the per-request text kept in the
    result file. Left alone it retains every prompt and completion in full, and a
    long run's JSON grows without a ceiling.
    """
    return _run(
        ctx,
        model=cell.model.name,
        tokenizer=cell.model.tokenizer,
        input_tokens=cell.input_tokens,
        output_tokens=cell.output_tokens,
        profile=f"kind=concurrent,streams={cell.load.concurrency}",
        override=None,
        requests_per_point=cell.load.requests,
        seconds_per_point=cell.load.seconds,
    )


def sweep_command(ctx: Context, spec: SweepSpec) -> Invocation:
    """One run that walks a concurrency ladder, one benchmark per rung.

    This is GuideLLM earning its place: N load points, one process, one file, one
    tokenizer load, and the endpoint sees a single continuous client rather than N
    cold starts. The rungs are ours, so nothing here overloads by design.

    The request budget is per rung, not per run — `max_requests` is a per-benchmark
    constraint — so a four-rung ladder at 20 requests sends 80. Stating it here
    because the arithmetic is the difference between a polite suite and a surprise.
    """
    rungs = ",".join(str(rung) for rung in spec.ladder)
    return _run(
        ctx,
        model=spec.model.name,
        tokenizer=spec.model.tokenizer,
        input_tokens=spec.input_tokens,
        output_tokens=spec.output_tokens,
        profile=f"kind=concurrent,streams={spec.ladder[0]}",
        override=("profile.streams", rungs),
        requests_per_point=spec.requests_per_rung,
        seconds_per_point=spec.seconds_per_rung,
    )


def sweep_read(ctx: Context, spec: SweepSpec) -> list[Emission]:
    """Every rung the ladder actually walked."""
    return _emissions(ctx)


def _run(
    ctx: Context,
    *,
    model: str,
    tokenizer: str,
    input_tokens: int,
    output_tokens: int,
    profile: str,
    override: tuple[str, str] | None,
    requests_per_point: int,
    seconds_per_point: int,
) -> Invocation:
    backend = (
        "kind=openai_http,"
        f"target={ctx.endpoint_url},"
        f"model={model},"
        f"api_key=${TOKEN_ENV},"
        "validate_backend=false"
    )
    parts = [
        "guidellm run",
        f'--backend "{backend}"',
        f'--data "kind=synthetic_text,prompt_tokens={input_tokens},output_tokens={output_tokens}"',
        f'--tokenizer "kind=huggingface_auto,model={tokenizer}"',
        f'--profile "{profile}"',
    ]
    if override:
        parts.append(f'--override "{override[0]}" "{override[1]}"')
    if requests_per_point:
        parts.append(f'--constraint "kind=max_requests,count={requests_per_point}"')
    if seconds_per_point:
        parts.append(f'--constraint "kind=max_duration,seconds={seconds_per_point}"')
    parts += [
        '--metrics "kind=generative,sample_size=50"',
        f'--output "kind=json,path={RESULTS}/{EXPORT}"',
        f'--output "kind=csv,path={RESULTS}/benchmarks.csv"',
        "--disable-console-interactive",
        "--disable-progress",
    ]
    return shell_command(ctx, " ".join(parts), mount_results_at=RESULTS, entrypoint="bash")


def _strategy(benchmark: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """What load point one benchmark in the file actually ran at."""
    strategy = ((benchmark.get("config") or {}).get("strategy")) or {}
    kind = str(strategy.get("type_") or "unknown")
    streams = strategy.get("streams")
    rate = strategy.get("rate")
    if streams:
        return f"c{int(streams)}", {"kind": "concurrency", "concurrency": int(streams)}
    if rate:
        return f"{float(rate):g} req/s", {"kind": "request_rate", "request_rate": float(rate)}
    if kind == "synchronous":
        return "c1", {"kind": "concurrency", "concurrency": 1}
    return kind, {"kind": kind}


def read(cell: Cell, ctx: Context) -> list[Emission]:
    """Every benchmark in the file, one emission each.

    The load point is read from what the harness recorded rather than from what the
    cell asked for. In a ladder or a sweep they are not the same thing, and in a
    sweep the rate was chosen by the tool at runtime.
    """
    return _emissions(ctx)


def _emissions(ctx: Context) -> list[Emission]:
    payload = read_json(ctx.results_dir / EXPORT)
    version = str((payload.get("metadata") or {}).get("guidellm_version") or PINNED_VERSION)
    benchmarks = payload.get("benchmarks")
    if not isinstance(benchmarks, list) or not benchmarks:
        raise ValueError(f"{EXPORT} holds no benchmarks")
    emissions: list[Emission] = []
    for benchmark in benchmarks:
        label, load = _strategy(benchmark)
        emissions.append(
            Emission(
                summary=benchmark,
                version=version,
                load_label=label,
                load=load,
                notes=[
                    "input token counts include the chat template, so they read higher than "
                    "the raw prompt budget and higher than AIPerf's for the same request"
                ],
            )
        )
    return emissions


HARNESS = Harness(
    name="guidellm",
    image=IMAGE,
    pinned_version=PINNED_VERSION,
    command=command,
    read=read,
    description="GuideLLM. Multi-point by design: a whole load ladder in one process, one file.",
    sweep_command=sweep_command,
    sweep_read=sweep_read,
    notes=(
        "Its `sweep` profile self-calibrates between the serial and the saturated rate, and its "
        "second leg is a deliberate overload — hence the explicit ladder by default.",
        "Reads only the `successful` split of every metric, so a latency here never averages in "
        "the requests that failed.",
    ),
)
