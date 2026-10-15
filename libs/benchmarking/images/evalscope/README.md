# evalscope

[EvalScope](https://github.com/modelscope/evalscope) `1.11.1`, wrapped for the cluster benchmark path.

## Why

A run grades a serving surface against accuracy datasets such as `mmlu_pro`, `gpqa_diamond`,
`aime2025`, `hle`, `longbench_v2`, `livecodebench` and `tau2_bench`, and EvalScope already speaks
that list against an OpenAI-compatible endpoint. This image is a thin carrier: the pinned
`evalscope` wheel, a shell, and nothing else. The CLI line is composed plane-side, in
[`harnesses.py:_evalscope_command`](../../../plane/app/modules/benchmarks/harnesses.py),
so a new evalscope release is a plane edit plus an image bump, not a new image shape.

## Build

```sh
just build evalscope
# or
docker build -t hangar-evalscope:1.11.1 .
```

The plane has no default for this image. Push it to a registry your clusters can pull from and
set `HANGAR_BENCHMARK_EVALSCOPE_IMAGE` on the plane, or pass `image` with each run; a run with
neither is refused with a message naming the setting.

## Run

The image's entrypoint is `sh`. The operator runs the benchmark wrapper under it:

```sh
docker run --rm hangar-evalscope:1.11.1 -c 'evalscope eval --help'
```

In a real run the wrapper is the generic sentinel-framed one in
[`render/benchmark.go:genericScript`](../../../operator/internal/render/benchmark.go):
the plane sends a `command` parameter (`evalscope eval --model ... --api-url ...`), the
operator tees the harness's stdout to a log, frames the summary between
`---HANGAR-BENCHMARK-SUMMARY-BEGIN---` / `---HANGAR-BENCHMARK-SUMMARY-END---`, and the
plane reads it back off the pod's log.

## Reading the summary

EvalScope 1.11 writes one report per dataset at `<work-dir>/reports/<model>/<dataset>.json`.
The plane sends `report_glob` pointing at those files; the wrapper merges them into
`{"reports": [<report>, ...]}` and frames that. The plane's `summaries.dataset_scores` reads
each report into a per-dataset metric and score.

## What this image is not

- Not a job spec. The Job is rendered by the operator, with this image named as its container.
- Not a dashboard. This image is for execution only; browse results with whatever reads
  EvalScope's output.
