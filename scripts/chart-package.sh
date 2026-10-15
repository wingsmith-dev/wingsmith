#!/usr/bin/env bash
# Packages the chart the way a release publishes it, without pushing anything.
#
#   chart-package.sh VERSION OUTDIR [chart-dir]     chart-dir defaults to libs/chart/cluster-setup
#
# Fails unless Chart.yaml's version and appVersion both equal VERSION. appVersion is the image tag
# the chart defaults to, and a release publishes images at its own version and no other tag, so
# any other appVersion installs images that do not exist.
#
# `helm dependency build` resolves an https dependency only through a configured repository, so
# each one Chart.yaml names is added first, to a repository config of its own that leaves the
# caller's helm setup alone. OCI dependencies need no repository.
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: chart-package.sh VERSION OUTDIR [chart-dir]" >&2
  exit 2
fi
version="$1"
out="$2"
root="$(git rev-parse --show-toplevel)"
chart="${3:-$root/libs/chart/cluster-setup}"

# A top-level scalar of Chart.yaml, unquoted.
field() {
  sed -n "s/^$1:[[:space:]]*//p" "$chart/Chart.yaml" | head -1 | tr -d "\"'" | sed 's/[[:space:]]*#.*//'
}

failed=0
for key in version appVersion; do
  got="$(field "$key")"
  if [ "$got" != "$version" ]; then
    echo "chart-package: $chart/Chart.yaml has $key '$got'; it must be '$version'" >&2
    failed=1
  fi
done
[ "$failed" -eq 0 ] || exit 1

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export HELM_REPOSITORY_CONFIG="$work/repositories.yaml"
export HELM_REPOSITORY_CACHE="$work/cache"

n=0
while read -r url; do
  n=$((n + 1))
  helm repo add "dependency-$n" "$url" >/dev/null
done < <(sed -n 's/^[[:space:]]*repository:[[:space:]]*["'\'']\{0,1\}\(https\{0,1\}:[^"'\'' ]*\).*/\1/p' "$chart/Chart.yaml" | sort -u)

# A copy, so the downloaded subchart tarballs never land in the working tree.
cp -R "$chart" "$work/chart"
helm dependency build "$work/chart" >/dev/null
mkdir -p "$out"
helm package "$work/chart" -d "$out"
