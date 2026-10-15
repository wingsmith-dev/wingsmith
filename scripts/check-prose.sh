#!/usr/bin/env bash
# Fails on text that belongs to one deployment's history rather than to the code.
#
#   check-prose.sh staged   lines added in the index (pre-commit)
#   check-prose.sh tree     every tracked file (CI)
#
# What it refuses:
#   names      internal cluster and build names, in a path or a line
#   assistant  the name of the coding assistant a change was written with
#   dates      an ISO date in a comment or docstring of non-test source: a comment states the
#              invariant, and when somebody measured it belongs in the commit that changed it
#   runtime    references to hangar-runtime, which is not part of this repository
#
# A correct hit is allowed in scripts/prose-allowlist.txt (or $HANGAR_PROSE_ALLOWLIST): one
# extended regex per line, matched against the reported `path:line:text`.
set -euo pipefail

mode="${1:-staged}"
root="$(git rev-parse --show-toplevel)"
cd "$root"
allowlist="${HANGAR_PROSE_ALLOWLIST:-$root/scripts/prose-allowlist.txt}"

# Word edges written out rather than \b or -w, which BSD and GNU grep disagree on.
edge='[^[:alnum:]_]'
names="(^|$edge)(site-a|site-b|site-c|site-d|cluster-a|model-a|patched|example)($edge|\$)"
assistant='Claude Code'
runtime='hangar-runtime'
# A calendar date, not a timestamp layout or an RFC 3339 example value.
date='(^|[^0-9])20[0-9][0-9]-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])([^0-9T]|$)'

source_files='\.(go|py|ts|tsx|js|mjs|sh|bash|ya?ml|tpl)$|(^|/)(Dockerfile|Makefile|justfile)$'
test_files='_test\.(go|py|sh)$|(^|/)(tests?|testdata|__tests__)/|(^|/)(test_[^/]*|conftest)\.py$|\.(test|spec)\.[jt]sx?$'

own=(scripts/check-prose.sh scripts/check-prose_test.sh scripts/prose-allowlist.txt)

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Drops lines whose path (the text before the first colon) is one of this guard's own files.
not_own() {
  awk -v own="${own[*]}" '
    BEGIN { n = split(own, o, " "); for (i = 1; i <= n; i++) skip[o[i]] = 1 }
    { p = substr($0, 1, index($0, ":") - 1); if (!(p in skip)) print }
  '
}

# The candidate lines as `path:line:text`, and the candidate paths as `path:0:path` so a file named
# after a cluster is caught as well as a line that names one.
case "$mode" in
  staged)
    git diff --cached -U0 --no-color --no-ext-diff --src-prefix=a/ --dst-prefix=b/ --diff-filter=ACMR |
      awk '
        /^\+\+\+ / { path = substr($0, 7); next }
        /^@@ / { split($3, a, ","); line = substr(a[1], 2) + 0; next }
        /^\+/ { print path ":" line ":" substr($0, 2); line++ }
      ' | not_own >"$work/lines"
    git diff --cached --name-only --diff-filter=ACMR >"$work/paths"
    ;;
  tree)
    git ls-files >"$work/paths"
    ;;
  *)
    echo "usage: check-prose.sh staged|tree" >&2
    exit 2
    ;;
esac
awk '{ print $0 ":0:" $0 }' "$work/paths" | not_own >"$work/pathlines"

# Lines matching an extended regex, from the index or from the whole tree.
scan_lines() {
  if [ "$mode" = tree ]; then
    { git grep -nI "$@" || true; } | not_own
  else
    grep "$@" "$work/lines" || true
  fi
}

scan_paths() {
  grep "$@" "$work/pathlines" || true
}

allowed() {
  if [ -s "$allowlist" ]; then
    grep -vE -f <(grep -v -e '^[[:space:]]*$' -e '^#' "$allowlist") || true
  else
    cat
  fi
}

failed=0
report() {
  local check="$1" why="$2" hits
  hits="$(allowed <"$work/hits")"
  if [ -n "$hits" ]; then
    echo "check-prose: $check: $why" >&2
    printf '%s\n' "$hits" | sed 's/^/  /' >&2
    failed=1
  fi
}

{ scan_lines -iE -e "$names"; scan_paths -iE -e "$names"; } >"$work/hits"
report names "internal cluster or build name"

{ scan_lines -F -e "$assistant"; scan_paths -F -e "$assistant"; } >"$work/hits"
report assistant "name of the coding assistant"

{ scan_lines -F -e "$runtime"; scan_paths -F -e "$runtime"; } >"$work/hits"
report runtime "hangar-runtime is not part of this repository"

scan_lines -E -e "$date" |
  awk -v date="$date" -v src="$source_files" -v tests="$test_files" -v quote="[\"'#]" '
    {
      i = index($0, ":"); path = substr($0, 1, i - 1)
      rest = substr($0, i + 1); text = substr(rest, index(rest, ":") + 1)
    }
    path !~ src || path ~ tests { next }
    !match(text, date) { next }
    {
      before = substr(text, 1, RSTART)
      comment = before ~ /(\/\/|#|\/\*)/ || before ~ /^[[:space:]]*\*/
      docstring = path ~ /\.py$/ && (before !~ quote || before ~ /^[[:space:]]*[rbuRBU]?("""|'"'''"')/)
      if (comment || docstring) print
    }
  ' >"$work/hits"
report dates "ISO date in a source comment; keep the invariant, put the history in the commit"

exit "$failed"
