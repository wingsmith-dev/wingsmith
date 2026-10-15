#!/usr/bin/env bash
# Refusals the justfile's release and cluster recipes share. Each subcommand exits non-zero
# with a reason on stderr when the thing it guards is unsafe, and is silent otherwise.
#
#   guards.sh clean-tree [--ignored] [path...]   no uncommitted, untracked (or ignored) files
#   guards.sh e2e-context                         print the kube context e2e may use, or refuse
#   guards.sh image-unpublished <ref>             refuse if the image tag already exists
#   guards.sh chart-unpublished <ref> <version>   refuse if the chart version already exists
set -euo pipefail

die() {
  echo "error: $*" >&2
  exit 1
}

# A missing tag answers with one of these. Anything else (no login, no network) is not proof the
# tag is free, so it refuses too.
absent='manifest unknown|no such manifest|not found|404'

case "${1:-}" in
  clean-tree)
    shift
    flags=(--porcelain --untracked-files=all)
    if [ "${1:-}" = "--ignored" ]; then
      flags+=(--ignored)
      shift
    fi
    dirty="$(git status "${flags[@]}" -- "$@")"
    if [ -n "$dirty" ]; then
      echo "$dirty" >&2
      die "the tree is not clean${*:+ under $*}; commit, stash or remove the files above first"
    fi
    ;;

  e2e-context)
    # e2e patches a node's labels and allocatable and creates and deletes namespaces, so it runs
    # against a kind cluster unless somebody names another context on purpose.
    if [ -n "${HANGAR_E2E_CONTEXT:-}" ]; then
      context="$HANGAR_E2E_CONTEXT"
      kubectl config get-contexts "$context" >/dev/null 2>&1 || die "no kube context named '$context'"
    else
      context="$(kubectl config current-context 2>/dev/null || true)"
      [ -n "$context" ] || die "no current kube context; 'just cluster-up' creates a local one"
      case "$context" in
        kind-*) ;;
        *) die "refusing to run e2e against '$context': it is not a kind- context. Set HANGAR_E2E_CONTEXT=$context to run it there anyway" ;;
      esac
    fi
    echo "$context"
    ;;

  image-unpublished)
    ref="${2:?usage: guards.sh image-unpublished <ref>}"
    if out="$(docker manifest inspect "$ref" 2>&1)"; then
      die "$ref already exists; tags are never overwritten, push a new one"
    fi
    grep -qiE "$absent" <<<"$out" || die "could not tell whether $ref exists: $out"
    ;;

  chart-unpublished)
    ref="${2:?usage: guards.sh chart-unpublished <ref> <version>}"
    version="${3:?usage: guards.sh chart-unpublished <ref> <version>}"
    if out="$(helm show chart "$ref" --version "$version" 2>&1)"; then
      die "$ref:$version already exists; bump the chart version in Chart.yaml"
    fi
    grep -qiE "$absent" <<<"$out" || die "could not tell whether $ref:$version exists: $out"
    ;;

  *)
    echo "usage: guards.sh clean-tree [--ignored] [path...] | e2e-context | image-unpublished <ref> | chart-unpublished <ref> <version>" >&2
    exit 2
    ;;
esac
