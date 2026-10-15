# evalscope

[EvalScope](https://github.com/modelscope/evalscope) `1.11.1`, wrapped for the plane's cluster
benchmark path. `hgrbench` does not use this image; the plane's `evalscope` harness does, and
it lives here beside the other harness images so all of them build from one place.

## Why

An accuracy run grades a serving surface against standard datasets (`mmlu_pro`,
`gpqa_diamond`, `aime2025` and the like), and EvalScope already speaks that list against an
OpenAI-compatible endpoint. This image is a thin carrier: the pinned `evalscope` wheel, a
shell, and nothing else. The whole CLI shape is composed plane-side, in
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
[`render/benchmark.go:genericScript`](../../../operator/internal/render/benchmark.go): the
plane sends a `command` parameter (`evalscope eval --model ... --api-url ...`) and a
`report_glob`, the operator runs the command, merges every per-dataset report EvalScope wrote
under its work directory into one `{"reports": [...]}` document, frames it between
`---HANGAR-BENCHMARK-SUMMARY-BEGIN---` / `---HANGAR-BENCHMARK-SUMMARY-END---`, and the plane
reads it back off the pod's log with `summaries.dataset_scores`.

A version bump on the harness must not silently rewrite the shape the plane reads, so any
adaptation of EvalScope's report happens in the plane's reader, not in this image.

## What this image is not

- Not a job spec. The Job is rendered by the operator, with this image named as its container.
- Not a dashboard. This image is for execution only; browse results with whatever reads
  EvalScope's output.
