"""`vllm bench serve`, from a client-only image we build ourselves.

The upstream way to get this tool is `vllm/vllm-openai`, which is 30.6 GB on disk
and carries a CUDA runtime to run a load generator that never touches a GPU. The
image in `libs/benchmarking/images/vllm-bench/` is the same benchmark sources at a
pinned tag, on `python:3.12-slim`, with the torch-dependent vllm internals replaced
by stubs — about 900 MB, and native on both architectures.

What this harness adds over AIPerf is the open loop. AIPerf holds N requests in
flight; this can hold an *arrival rate* instead, Poisson-shaped, which is what a
real caller population looks like and what makes a queue form. Both modes are here:
`--request-rate inf --max-concurrency N` is closed-loop, a `--request-rate` is open.

Three of its flags are not optional in practice, and each one was a failed run
first:

- `--model` must be passed. Left out, it auto-discovers from `/v1/models` and reads
  `data[0]["root"]`, which our gateway does not emit — `KeyError: 'root'`.
- `--backend openai-chat` **and** `--endpoint /v1/chat/completions` must agree. The
  defaults are the completions pair, and a mismatch sends chat bodies to the
  completions path where every request fails as a 4xx.
- `--percentile-metrics` must name `e2el` and `--metric-percentiles` must name the
  percentiles. The defaults are `ttft,tpot,itl` at p99 only, so a default run has no
  median, no p90, and no end-to-end latency at all.

Two keys in its output are deliberately not read by `metrics`:
`max_concurrent_requests` and `max_output_tokens_per_s` are computed by binning
requests into integer-second buckets, which on a short run makes the first equal to
the request count and can make the second come out *below* the mean.
"""

from __future__ import annotations

from hgrbench.plan import Cell
from hgrbench.harnesses.base import TOKEN_ENV
from hgrbench.harnesses.base import Context
from hgrbench.harnesses.base import Harness
from hgrbench.harnesses.base import Emission
from hgrbench.harnesses.base import Invocation
from hgrbench.harnesses.base import quote
from hgrbench.harnesses.base import read_json
from hgrbench.harnesses.base import shell_command


IMAGE = "hangar/vllm-bench:0.28.0"
PINNED_VERSION = "0.28.0"
BUILD_CONTEXT = "libs/benchmarking/images/vllm-bench"
RESULTS = "/results"
EXPORT = "run.json"
ENTRYPOINT = "/bench/main.py"


def command(cell: Cell, ctx: Context) -> Invocation:
    """One `vllm bench serve` run at one operating point.

    Driven through a shell inside the container so that the token can be expanded
    there, into `--header`. `OPENAI_API_KEY` would also work for the load requests
    and is tempting for being one fewer moving part, but it is not applied to
    `/v1/models` or `/tokenize` — and a credential that covers some of a tool's
    requests and not others is a trap for whoever changes a flag later.

    `--seed 0` and `--temperature 0` are here because a benchmark that cannot be
    repeated is an anecdote. vLLM no longer sends a temperature by default and says
    so in a warning; left to the server, two runs are not comparable.
    """
    args = [
        f"python3 {ENTRYPOINT}",
        "--backend openai-chat",
        f"--base-url {quote(ctx.endpoint_url)}",
        "--endpoint /v1/chat/completions",
        f"--model {quote(cell.model.name)}",
        f"--tokenizer {quote(cell.model.tokenizer)}",
        "--dataset-name random",
        f"--random-input-len {cell.input_tokens}",
        f"--random-output-len {cell.output_tokens}",
        f"--num-prompts {cell.load.requests}",
        f"--max-concurrency {cell.load.concurrency}",
        "--ignore-eos",
        "--temperature 0",
        "--seed 0",
        "--percentile-metrics ttft,tpot,itl,e2el",
        "--metric-percentiles 50,90,99",
        f"--metadata version={PINNED_VERSION}",
        "--save-result",
        "--save-detailed",
        f"--result-dir {RESULTS}",
        f"--result-filename {EXPORT}",
        "--disable-tqdm",
        f'--header "Authorization=Bearer ${TOKEN_ENV}"',
    ]
    if cell.load.request_rate:
        args += [f"--request-rate {cell.load.request_rate:g}", "--burstiness 1.0"]
    else:
        args += ["--request-rate inf"]
    return shell_command(ctx, " ".join(args), mount_results_at=RESULTS)


def read(cell: Cell, ctx: Context) -> list[Emission]:
    """The flat JSON it writes, and the vendored tag that produced it.

    Its exit code says nothing about the run: pointed at a closed port it prints
    `All requests failed` and exits 0, because `main` returns a result dict either
    way. `completed` and `failed` in this file are the only truth about whether
    anything was measured, and the runner grades the cell on them.
    """
    summary = read_json(ctx.results_dir / EXPORT)
    version = str(summary.get("version") or PINNED_VERSION)
    notes: list[str] = []
    requested = cell.input_tokens
    realised = summary.get("total_input_tokens")
    completed = summary.get("completed")
    if isinstance(realised, (int, float)) and isinstance(completed, (int, float)) and completed:
        mean_isl = realised / completed
        if requested and abs(mean_isl - requested) / requested > 0.05:
            notes.append(
                f"asked for {requested} input tokens and sent {mean_isl:.0f}: the random dataset "
                "decodes sampled token ids back to text, and the chat template inflates the count"
            )
    kind = "request_rate" if cell.load.request_rate else "concurrency"
    return [
        Emission(
            summary=summary,
            version=version,
            load_label=cell.load.label,
            load={
                "kind": kind,
                "concurrency": cell.load.concurrency,
                "request_rate": cell.load.request_rate,
            },
            notes=notes,
        )
    ]


HARNESS = Harness(
    name="vllm-bench",
    image=IMAGE,
    pinned_version=PINNED_VERSION,
    command=command,
    read=read,
    description="vLLM's own `bench serve`. Open-loop capable: an arrival rate, not just a concurrency.",
    build_context=BUILD_CONTEXT,
    notes=(
        "Built here rather than pulled: the official image is 30.6 GB to run a client.",
        "Its realised input length exceeds the requested one, so the report reads the input "
        "length it says it sent, never the flag.",
    ),
)
