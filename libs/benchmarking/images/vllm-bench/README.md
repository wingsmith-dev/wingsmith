# vllm-bench

`vllm bench serve` and `vllm bench sweep` with no vLLM engine and no GPU.

Pinned upstream tag: **v0.28.0** (set once, at the top of `vendor.sh`).

## Why

The official `vllm/vllm-openai:v0.28.0` is 30.6 GB on disk and wants a GPU, because the benchmark
code ships inside the serving image. We only ever run the benchmark as a *client* against a gateway
we did not start. This image is 996 MB: `python:3.12-slim`, the upstream benchmark sources, and
hand-written stubs in place of the torch-dependent vLLM internals they import.

Two source trees, one runtime tree:

| Dir | Committed | What |
|---|---|---|
| `upstream/` | no (gitignored) | fetched verbatim by `vendor.sh` |
| `stubs/` | yes | `vllm.{logger,tokenizers,inputs,lora,multimodal,transformers_utils,utils}` — the part that is not reproducible |

The Dockerfile COPYs both into `/bench`, so the split is visible in the repo and invisible at runtime.

## What the stubs give up

- `fetch_image` / `fetch_audio` raise `NotImplementedError`. The `random-mm`, `custom_image`,
  `custom_audio` and ASR datasets are out. Text datasets are unaffected.
- `FlexibleArgumentParser` is a plain `argparse.ArgumentParser` subclass, so upstream's
  `--config <yaml>` feature is gone. Pass flags.
- `vllm bench sweep startup` is out: it needs `vllm.benchmarks.startup`, which starts an engine.
  `sweep.py` therefore has four subcommands, not five.

## Build

```sh
./vendor.sh                              # fetch upstream/ at the pinned tag
docker build -t hangar/vllm-bench:0.28.0 .
```

`vendor.sh` writes only under `upstream/`. It never touches `stubs/`.

## Run

Two entrypoints. The default one is the single-point load generator; the sweep is reached by
overriding the entrypoint.

**Single point** — `vllm bench serve`:

```sh
docker run --rm -e TOK -e HF_HOME=/hf \
  -v ~/.cache/huggingface:/hf -v "$PWD/results:/results" \
  --entrypoint sh hangar/vllm-bench:0.28.0 -c '
python /bench/main.py \
  --backend openai-chat --base-url http://host.docker.internal:8080 \
  --endpoint /v1/chat/completions \
  --model gpt-oss-120b --tokenizer openai/gpt-oss-120b \
  --dataset-name random --random-input-len 128 --random-output-len 32 \
  --num-prompts 2 --max-concurrency 1 --ignore-eos --temperature 0 \
  --percentile-metrics ttft,tpot,itl,e2el --metric-percentiles 50,90,99 \
  --save-result --result-dir /results \
  --header "Authorization=Bearer ${TOK}"
'
```

`docker run hangar/vllm-bench:0.28.0 <flags>` is equivalent when no header needs a secret: the
default entrypoint is `python /bench/main.py`.

Flags this gateway requires: `--backend openai-chat --endpoint /v1/chat/completions`, and `--model`
with the *served* name — omitting it crashes with `KeyError: 'root'`, because the gateway's
`/v1/models` has no `root` field. `--tokenizer` takes the HF repo id.

**Sweep** — `vllm bench sweep {serve,serve_workload,plot,plot_pareto}`:

```sh
docker run --rm -e TOK -e HF_HOME=/hf \
  -v ~/.cache/huggingface:/hf -v "$PWD:/work" \
  --entrypoint python hangar/vllm-bench:0.28.0 /bench/sweep.py serve \
  --serve-cmd "python3 /bench/fakehealth.py --host 127.0.0.1 --port 9000" \
  --after-bench-cmd "true" \
  --bench-cmd "python3 /bench/main.py --backend openai-chat ..." \
  --bench-params /work/bench_params.json \
  --num-runs 1 -o /work/results -e exp1
```

`sweep serve` requires `--serve-cmd`, and probes `GET /health` on the host and port it parses out of
that string before every run. We are benchmarking something we did not start, so point `--serve-cmd`
at `fakehealth.py` — a stdlib server that answers 200 to any GET or POST — and point the bench
command's `--base-url` at the real gateway. Pass `--after-bench-cmd "true"` as well, or
`after_bench()` POSTs `/reset_prefix_cache` at the fake server and raises
`NotImplementedError: No implementation of reset_caches`.

`--bench-params` is a JSON object mapping a run name to a dict of flags:

```json
{
  "c1_isl128_osl32": {"max_concurrency": 1, "random_input_len": 128, "random_output_len": 32, "num_prompts": 2},
  "c2_isl128_osl32": {"max_concurrency": 2, "random_input_len": 128, "random_output_len": 32, "num_prompts": 2}
}
```

Output, for `-o /work/results -e exp1`:

```
results/exp1/BENCH--<name>/run=0.json     one run
results/exp1/BENCH--<name>/summary.json   list of that name's runs
results/exp1/summary.csv                  every run, one row each
```

`sweep serve_workload` names its dirs `BENCH--<var>=<v>-WL--<var>=<v>` instead. `sweep plot` writes
`<exp>/FIGURE.png`; `sweep plot_pareto` writes `<exp>/pareto/PARETO.png`.

## Driving it from Python

- **The exit code is not the result.** `vllm bench serve` exits 0 even when every request failed —
  it warns `All requests failed` and returns a result dict anyway. Read `completed` and `failed` out
  of the JSON. `sweep serve` does propagate a non-zero exit when the bench command itself fails.
- Metrics reach the filesystem only with `--save-result`; without it they are stdout tables. The
  sweep adds `--save-result` to the bench command for you.
- The container runs as **root**, so results land root-owned on a Linux host bind mount. On Docker
  Desktop they appear as the host user.
- `summary.csv` is written by pandas at runtime, and the sweep imports pandas eagerly enough that
  `run_combs` dies at `pd.DataFrame.from_records` without it. It is in the image. `plot` and
  `plot_pareto` additionally need matplotlib and seaborn; also in the image. No scipy — nothing
  vendored here imports it.
- The sweep echoes the whole bench command to stdout, including `--header "Authorization=Bearer …"`.
  Do not log its output anywhere the token should not be.
