#!/usr/bin/env bash
# Renders the chart and validates every object against the Kubernetes schemas with kubeconform.
#
#   chart-kubeconform.sh [chart-dir]     default libs/chart/cluster-setup
#
# Two renders: the defaults with only the required blanks filled, and one with every optional
# feature on (serving, monitoring, the HF token Secret). The subcharts are left out, as in
# `just chart-lint`: their tarballs are not vendored, and they are validated upstream.
# Needs helm, kubeconform and uv on PATH.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
chart="${1:-$root/libs/chart/cluster-setup}"
# The oldest Kubernetes the chart accepts (Chart.yaml kubeVersion).
kube_version="${KUBE_VERSION:-1.29.0}"
# Schemas for the third-party kinds the chart renders (cert-manager, Traefik), at a fixed commit.
crds_catalog="https://raw.githubusercontent.com/datreeio/CRDs-catalog/d373c2da9702bc9509a004db83e57263fe3bdfc1"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp -R "$chart" "$work/chart"
uv run --no-project --quiet --with pyyaml python - "$work/chart/Chart.yaml" <<'PY'
import pathlib, sys, yaml
path = pathlib.Path(sys.argv[1])
chart = yaml.safe_load(path.read_text())
chart.pop("dependencies", None)
path.write_text(yaml.safe_dump(chart, sort_keys=False))
PY
rm -f "$work/chart/Chart.lock"

required=(
  --set cluster.id=ci
  --set cluster.hubUrl=wss://hangar.example.com/v1/hub/sync
  --set enrolmentToken.value=hgrtok_ci
  --set pools.labelKey=example.com/pool --set pools.gpuValue=gpu --set pools.cacheValue=system
  --set modelCache.storageClass=standard
)
everything=(
  --set serving.enabled=true --set serving.host=hangar.example.com
  --set serving.clusterIssuer=letsencrypt --set serving.backend.service=frontend
  --set monitoring.enabled=true --set cluster.region=ci-region-1
  --set monitoring.remoteWrite.url=https://metrics.example.com/api/v1/write
  --set hfToken.create=true --set hfToken.value=hf_ci
)

helm template hangar "$work/chart" -n hangar-system "${required[@]}" >"$work/defaults.yaml"
helm template hangar "$work/chart" -n hangar-system "${required[@]}" "${everything[@]}" >"$work/everything.yaml"

for render in defaults everything; do
  echo "── $render"
  kubeconform -strict -summary -kubernetes-version "$kube_version" \
    -schema-location default \
    -schema-location "$crds_catalog/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
    "$work/$render.yaml"
done
