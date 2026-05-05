"""`vllm bench sweep serve` — upstream's parameter sweep, pointed at a server we do not own.

The sweep was built to launch one vLLM server per parameter combination, and on the
face of it that puts it out of reach: we have no GPU, and the endpoint is a Dynamo
deployment behind a gateway. `--serve-cmd` is `required=True`.

It is reachable anyway, and the reason is a seam in upstream's own design: the
server it launches and the server it benchmarks are addressed separately.
`sweep/server.py` builds its readiness probe from the `--host`/`--port` it parses
out of `--serve-cmd` itself, while the benchmark goes wherever `--bench-cmd` says.
So a fifteen-line HTTP server that answers 200 satisfies the probe, and the load
goes to the real endpoint. `--after-bench-cmd "true"` is the second half of the
trick: left to itself, `after_bench()` POSTs `/reset_prefix_cache` at whatever it
launched and raises `NotImplementedError` when that is not a vLLM.

What this buys over looping `vllm-bench` ourselves — which is what the matrix
already does — is upstream's own artifacts: `summary.json` per combination,
`summary.csv` over the whole experiment, and `sweep plot`. When somebody asks
whether our numbers match what vLLM's own tooling reports, this is the harness that
answers, because it *is* vLLM's own tooling.

The sweep is a matrix in `--bench-params`, one entry per combination, and the flags
in each entry are `vllm bench serve` flags. We write one entry per rung of the
ladder.
"""

from __future__ import annotations

import json

from typing import Any

from hgrbench.plan import SweepSpec
from hgrbench.harnesses.base import TOKEN_ENV
from hgrbench.harnesses.base import Context
from hgrbench.harnesses.base import Harness
from hgrbench.harnesses.base import Emission
from hgrbench.harnesses.base import Invocation
from hgrbench.harnesses.base import HarnessError
from hgrbench.harnesses.base import read_json
from hgrbench.harnesses.base import shell_command
from hgrbench.harnesses.vllm_bench import IMAGE
from hgrbench.harnesses.vllm_bench import BUILD_CONTEXT
from hgrbench.harnesses.vllm_bench import PINNED_VERSION


RESULTS = "/results"
EXPERIMENT = "sweep"
FAKE_HEALTH_PORT = 9000
SERVE_ENTRYPOINT = "/bench/main.py"
SWEEP_ENTRYPOINT = "/bench/sweep.py"
FAKE_HEALTH = "/bench/fakehealth.py"


def bench_params(spec: SweepSpec) -> dict[str, dict[str, Any]]:
    """The sweep's matrix: one combination per rung of the ladder.

    Named `c{N}_isl{I}_osl{O}` because the name becomes the directory the results
    land in, and a directory name that does not say what it measured is a directory
    somebody has to open to identify.
    """
    matrix: dict[str, dict[str, Any]] = {}
    for rung in spec.ladder:
        name = f"c{rung}_isl{spec.input_tokens}_osl{spec.output_tokens}"
        matrix[name] = {
            "max_concurrency": rung,
            "random_input_len": spec.input_tokens,
            "random_output_len": spec.output_tokens,
            "num_prompts": spec.requests_per_rung,
        }
    return matrix


def sweep_command(ctx: Context, spec: SweepSpec) -> Invocation:
    """One sweep over the whole ladder for one model.

    The bench command is a string the sweep appends its own flags to — it adds
    `--percentile-metrics ttft,tpot,itl,e2el --save-result --result-dir <dir>
    --result-filename run=N.json`, plus one `--<key> <value>` per matrix entry. So
    this side must not pass any of those, and must pass everything else.

    The token is written as `${HGR_BENCH_TOKEN}` inside a single-quoted `--header`,
    which the container's shell expands when the sweep spawns the bench command.
    """
    params = ctx.results_dir / "bench_params.json"
    params.write_text(json.dumps(bench_params(spec), indent=2))

    bench = " ".join(
        [
            f"python3 {SERVE_ENTRYPOINT}",
            "--backend openai-chat",
            f"--base-url {ctx.endpoint_url}",
            "--endpoint /v1/chat/completions",
            f"--model {spec.model.name}",
            f"--tokenizer {spec.model.tokenizer}",
            "--dataset-name random",
            "--ignore-eos",
            "--temperature 0",
            "--seed 0",
            "--metric-percentiles 50,90,99",
            "--disable-tqdm",
            f"--header 'Authorization=Bearer ${TOKEN_ENV}'",
        ]
    )
    argv = " ".join(
        [
            f"python3 {SWEEP_ENTRYPOINT} serve",
            f'--serve-cmd "python3 {FAKE_HEALTH} --host 127.0.0.1 --port {FAKE_HEALTH_PORT}"',
            '--after-bench-cmd "true"',
            f'--bench-cmd "{bench}"',
            f"--bench-params {RESULTS}/bench_params.json",
            f"--num-runs {spec.num_runs}",
            f"-o {RESULTS}",
            f"-e {EXPERIMENT}",
        ]
    )
    return shell_command(ctx, argv, mount_results_at=RESULTS)


def _rung_of(name: str, spec: SweepSpec) -> int:
    """The concurrency a result directory's name encodes."""
    head = name.split("_", 1)[0]
    if head.startswith("c") and head[1:].isdigit():
        return int(head[1:])
    return spec.ladder[0]


def sweep_read(ctx: Context, spec: SweepSpec) -> list[Emission]:
    """One emission per run of per combination the sweep executed.

    `summary.json` is a JSON *list* — one element per `--num-runs` repetition, each
    the same flat schema `vllm bench serve` writes on its own. Every element becomes
    its own emission rather than being averaged here, because the repetitions are
    exactly the evidence for how much of a difference between two numbers is noise,
    and an average destroys it. The runner keeps them apart by `run_number`.
    """
    root = ctx.results_dir / EXPERIMENT
    if not root.is_dir():
        raise HarnessError(f"the sweep wrote no {EXPERIMENT}/ directory under {ctx.results_dir.name}")
    emissions: list[Emission] = []
    for bench_dir in sorted(root.glob("BENCH--*")):
        name = bench_dir.name.removeprefix("BENCH--")
        rung = _rung_of(name, spec)
        summary_path = bench_dir / "summary.json"
        runs: list[Any]
        if summary_path.exists():
            payload = read_json(summary_path)
            runs = payload if isinstance(payload, list) else [payload]
        else:
            runs = [read_json(path) for path in sorted(bench_dir.glob("run=*.json"))]
        for run in runs:
            if not isinstance(run, dict):
                continue
            emissions.append(
                Emission(
                    summary=run,
                    version=str(run.get("version") or PINNED_VERSION),
                    load_label=f"c{rung}",
                    load={
                        "kind": "concurrency",
                        "concurrency": rung,
                        "run_number": int(run.get("run_number") or 0),
                    },
                    notes=[f"upstream sweep combination {name}"],
                )
            )
    if not emissions:
        raise HarnessError(f"the sweep produced no readable combination under {root}")
    return emissions


def _unsupported(*_args: Any, **_kwargs: Any) -> Any:
    raise HarnessError(
        "vllm-sweep runs a whole ladder in one process and has no single-point mode. "
        "Use the vllm-bench harness for one operating point, or put vllm-sweep in [sweep]."
    )


HARNESS = Harness(
    name="vllm-sweep",
    image=IMAGE,
    pinned_version=PINNED_VERSION,
    command=_unsupported,
    read=_unsupported,
    description="vLLM's own parameter sweep, driven client-side against an endpoint it did not launch.",
    build_context=BUILD_CONTEXT,
    sweep_command=sweep_command,
    sweep_read=sweep_read,
    notes=(
        "Reachable only because upstream addresses the server it launches and the server it "
        "benchmarks separately: a fake local health endpoint satisfies the probe.",
        "Writes upstream's own summary.csv and can render upstream's own plots, which is the "
        "point of using it rather than looping vllm-bench.",
    ),
)
