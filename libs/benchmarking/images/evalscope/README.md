# evalscope

[EvalScope](https://github.com/modelscope/evalscope) `0.19.0`, wrapped for the cluster benchmark path.

## Why

A ``run`` wants to grade a serving surface against the datasets the acceptance standard
names — `mmlu_pro`, `gpqa_diamond`, `aime2025`, `hle`, `longbench_v2`, `livecodebench`,
`tau2_bench` — and EvalScope is the harness that already speaks that list against an
OpenAI-compatible endpoint. This image is a thin carrier: the pinned `evalscope` wheel, a
shell, and nothing else. The whole CLI shape is composed plane-side, in
[`harnesses.py:_evalscope_command`](../../../plane/app/modules/benchmarks/harnesses.py),
so a new evalscope release is a plane edit plus an image bump, not a new image shape.

## Build

```sh
docker build -t hangar-evalscope:0.19.0 .
```

The pin the plane names (`harnesses.EVALSCOPE.image`) is
`ghcr.io/hangar-dev/evalscope:0.19.0`. Push under that tag
when the plane's registry is ECR; see `just build evalscope` at the repository root.

## Run

The image's entrypoint is `sh`. The operator runs the benchmark wrapper under it:

```sh
docker run --rm hangar-evalscope:0.19.0 -c 'evalscope eval --help'
```

In a real run the wrapper is the generic sentinel-framed one in
[`render/benchmark.go:genericScript`](../../../operator/internal/render/benchmark.go):
the plane sends a `command` parameter (`evalscope eval --model ... --api-url ...`), the
operator tees the harness's stdout to a log, frames the last JSON block between
`---HANGAR-BENCHMARK-SUMMARY-BEGIN---` / `---HANGAR-BENCHMARK-SUMMARY-END---`, and the
plane reads it back off the pod's log.

## Reading the summary

The sentinel frame carries whatever JSON evalscope printed last. At `0.19.0` the per-dataset
report is a list of `{dataset, metric, score, n, stderr}` objects; the shape the plane's
`summaries.dataset_scores` reads is

```json
{"results": {"<dataset>": {"metric": "accuracy", "score": 0.7412, "n": 12032}}}
```

If evalscope's native print already has that shape, the wrapper's job is just the framing.
If it does not, the adaptation happens in the wrapper — not in evalscope — because a version
bump on the harness must not silently rewrite the shape the plane reads.

## What this image is not

- Not a job spec. The Job is rendered by the operator, with this image named as its container.
- Not a Gradio dashboard. This image is for execution only; browse results with whatever reads
  EvalScope's output.
