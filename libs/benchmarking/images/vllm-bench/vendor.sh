#!/usr/bin/env bash
# Fetch the upstream vLLM benchmark sources into ./upstream/ (gitignored).
#
# This script writes ONLY under upstream/. The hand-written stubs under stubs/
# replace the torch-dependent vllm internals and are never touched here.
set -euo pipefail

TAG="v0.28.0"

here="$(cd "$(dirname "$0")" && pwd)"
base="https://raw.githubusercontent.com/vllm-project/vllm/${TAG}/vllm/benchmarks"
dest="$here/upstream/vllm/benchmarks"

FILES=(
  __init__.py
  serve.py
  plot.py
  lib/__init__.py
  lib/endpoint_request_func.py
  lib/ready_checker.py
  lib/utils.py
  datasets/__init__.py
  datasets/datasets.py
  datasets/utils.py
  sweep/__init__.py
  sweep/param_sweep.py
  sweep/plot.py
  sweep/plot_pareto.py
  sweep/serve.py
  sweep/serve_workload.py
  sweep/server.py
  sweep/utils.py
)

rm -rf "$here/upstream"
mkdir -p "$dest/lib" "$dest/datasets" "$dest/sweep"
for f in "${FILES[@]}"; do
  curl -sfL -o "$dest/$f" "$base/$f"
done

echo "vendored vllm benchmarks + benchmarks/sweep at ${TAG} into upstream/"
