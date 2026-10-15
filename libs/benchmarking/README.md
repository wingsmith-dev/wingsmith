# benchmarking — four load generators, one comparable answer

`hgrbench` points the load generators this project cares about at an
OpenAI-compatible endpoint, from a laptop, in containers, one at a time — and puts
what they said on one page.

```sh
just bench-suite smoke              # ~10 minutes, safe any time of day
just bench-suite deep --hard        # ~2 hours, includes the saturation search
just bench-report                   # re-render the latest run
```

It exists because "is the endpoint fast?" is four different questions, and because
one tool's answer to any of them is not checkable. Four tools measuring the same
load point is.

Read [`METHOD.md`](METHOD.md) before quoting a number from it. It covers what a
load generator can and cannot tell you, and how to decide whether a difference
between two runs is real.

## What is here, and why four

| Harness | What it is for | Load model |
|---|---|---|
| `aiperf` | NVIDIA AIPerf. The reference, because the plane's own benchmarks module pins this exact image and version — a local number and a cluster number are then the same measurement. | closed loop, N in flight |
| `vllm-bench` | vLLM's `bench serve`. The only one here that can hold an **arrival rate** instead of a concurrency, Poisson-shaped, which is what makes a queue form the way real traffic does. | open or closed loop |
| `vllm-sweep` | vLLM's own parameter sweep, `bench sweep serve`. Walks a ladder in one process and writes upstream's own `summary.csv` and plots. When somebody asks whether our numbers match what vLLM's tooling reports, this is the answer, because it *is* vLLM's tooling. | ladder, one process |
| `guidellm` | GuideLLM. Multi-point by design: one process, one continuous client, one file for the whole ladder. Its `sweep` profile self-calibrates between the serial and the saturated rate. | ladder or self-calibrating sweep |

The three requested sets map onto this as: `vllm-bench` for the CLI, `vllm-sweep`
for the parameter sweep, `guidellm` for GuideLLM — with AIPerf kept as the control,
since it is what the cluster path already runs.

## The three stages of a run

1. **Matrix** — every (model × load point × harness × repeat), one cell at a time.
   This is what makes the harnesses comparable: they measure the same load point
   back to back, so a difference between them is about the tools and not about what
   else was happening.
2. **Ladders** — the harnesses that can walk a whole curve in one process do so.
   One tokenizer load, one client, no cold start per rung.
3. **The saturation search** — climb until p90 TTFT crosses the SLA or a request
   fails, then stop and report the last rung that held. This is the one stage that
   can degrade a live endpoint, so it is opt-in in the plan **and** needs `--hard`.

Everything is serial, deliberately. Two load generators against one deployment
measure each other.

## Plans, not flags

A plan is committed TOML — the endpoint, the models, the load points, the ladders,
the SLA. `plans/smoke.toml` and `plans/deep.toml` are the two profiles. Flags only
narrow a plan (`--harness`, `--model`), show it (`--dry-run`), or unlock the
saturation search (`--hard`).

That split is on purpose: what got measured is then reviewable in a diff, and a
number in a report can be traced to a document rather than to somebody's shell
history.

## How the numbers are made comparable

The same way the plane does it, on purpose — see
[`libs/plane/app/modules/benchmarks/summaries.py`](../plane/app/modules/benchmarks/summaries.py).

- Each harness's export is stored **verbatim**. Nothing is converted on the way in.
- Normalisation happens only through an explicit per-harness, per-version field map
  in [`hgrbench/metrics.py`](hgrbench/metrics.py): *for this harness, between these
  versions, p90 TTFT is at this path, in these units*.
- The field ids are the plane's ids. `ttft.p90` means the same thing in both places,
  so a map written here can be lifted into the plane when a harness graduates to
  running inside a cluster.
- A path that does not resolve is a **named miss**, not a silent zero. The report
  says which harness could not answer which question.
- A value computed rather than read is marked as derived, with its formula in the
  report's footnotes.

A correction to a field map is therefore a re-read of what is already on disk
(`hgrbench report <run>`), never a re-run against the endpoint.

## What the report is for

Four questions, in the order people ask them: what this run says, how the endpoint
behaves as load rises, whether the harnesses agree, and exactly what was measured.

The third one is the interesting one. Four harnesses at the same load point rarely
agree on every field: they build prompts differently, count tokens differently, and
define the first token differently. Aggregate throughput tends to agree closely and
time to first token tends not to. A single-harness number would hide that, and a
report that averaged them would invent a number that no tool measured, so the report
shows each harness's own value and the spread between them.

## Things that cost somebody an afternoon

- **AIPerf's amd64 image hangs under emulation on Apple silicon.** It starts its
  supervisor and never spawns a worker: no output, no CPU, no exit. Every image here
  is therefore pulled **by digest for the host architecture** — Docker will not swap
  variants for a tag it already holds.
- **`vllm bench serve` exits 0 when every request failed.** It returns its result
  dict either way. The runner grades a cell on `completed` in the export, not on the
  exit code.
- **The tokenizer is not the served model name.** All four harnesses count tokens
  client-side and default the tokenizer to `--model`; `gpt-oss-120b` is not a Hugging
  Face repository, so the run dies before it sends a request. Plans carry a tokenizer
  per model and the runner refuses a model without one.
- **GuideLLM's `validate_backend` defaults to true** and issues `GET /health` with
  `raise_for_status`. Our gateway answers 400 there with a token and 401 without, so
  the default aborts every run with a message about not being able to connect, which
  reads like a network fault and is not one.
- **GuideLLM's `max_requests` constraint is per benchmark, not per run.** A four-rung
  ladder at 20 requests sends 80.
- **GuideLLM's `sweep` profile overloads on purpose.** Its second leg is
  `throughput` with `max_concurrency` defaulting to 512 — that is how it finds the
  saturation point. Never point a default sweep at production; the ladder here is
  explicit for that reason.
- **The vLLM sweep echoes the whole bench command to stdout, token and all.** Logs
  are captured and the token redacted out before anything is written to disk.
- **Realised input lengths differ per harness.** vLLM's random dataset decodes
  sampled token ids back to text and the chat template inflates the count; GuideLLM
  reports template-inclusive counts. Reported input length is in the table for
  exactly this reason — never compare against the flag value.
- **`max_concurrent_requests` and `max_output_tokens_per_s` in vLLM's output are
  bucketed by integer second.** On a short run the first equals the request count and
  the second can come out below the mean. Neither is read.

## Layout

```
plans/                    committed suite plans (smoke, deep)
hgrbench/plan.py          plan file → the matrix, the ladders, the ladder specs
hgrbench/runner.py        one cell at a time; the saturation search
hgrbench/metrics.py       the field maps: harness export → canonical ids
hgrbench/store.py         .local/bench/<run>/ layout, suite.json
hgrbench/harnesses/       one module per load generator + the registry
hgrbench/report/          the page
images/vllm-bench/        the client-only vLLM image (see its own README)
```

No dependencies. Plans are TOML and `tomllib` is in the standard library, the report
is a string, and the harnesses are containers — so `python3 -m hgrbench` works from a
fresh checkout with no virtualenv.

## Adding a harness

Three things, and nothing else in the package is harness-specific:

1. A module in `hgrbench/harnesses/` with a `command(cell, ctx)` and a
   `read(cell, ctx)` — plus `sweep_command`/`sweep_read` if it can walk a ladder.
2. An `Adapter` in `hgrbench/metrics.py` naming where its numbers are, with a
   version range.
3. A line in `hgrbench/harnesses/registry.py`.

A harness with no adapter still runs and its export is still stored — it just
contributes nothing to the comparison, and the report says so by name.

## Not done yet

- GuideLLM's self-calibrating `sweep` profile is implemented but not wired to a plan
  key; the explicit ladder is what the plans use. It wants a `--hard` gate of its own.
- `vllm bench sweep serve_workload` works in the image and is not driven from here.
  It is upstream's replacement for the SLA finder and would be a second, sharper
  saturation search.
- Nothing here writes to the plane. The obvious next step is `hgrbench` submitting a
  run as a plane benchmark so a local investigation and a cluster benchmark land in
  one history — the field ids already match.
