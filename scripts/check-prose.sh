#!/usr/bin/env bash
# Fails on text that belongs to one deployment's history, or to one writer's habits, and not to
# the code.
#
#   check-prose.sh staged           lines added in the index (pre-commit)
#   check-prose.sh range BASE HEAD  lines a pull request adds (CI)
#   check-prose.sh tree             every tracked file (CI report)
#
# What it refuses:
#   names    words from a private list. The list names exactly what must not appear here, so it
#            cannot live here either: it is read from $WINGSMITH_PROSE_DENYLIST, default
#            ~/.config/wingsmith-oss/prose-denylist, one case-insensitive extended regex per line,
#            matched at word edges in a line or a path. Without the list this check is skipped.
#   history  in a comment or docstring of non-test source, a calendar date or a claim about what
#            production did. A comment states the invariant; when and where somebody measured it
#            belongs in the commit that changed it.
#   tics     the em dash and a few stock phrases (the list is under `tics` below).
#
# A correct hit is allowed in scripts/prose-allowlist.txt (or $WINGSMITH_PROSE_ALLOWLIST): one
# extended regex per line, matched against the reported `path:line:text`.
set -euo pipefail

mode="${1:-staged}"
root="$(git rev-parse --show-toplevel)"
cd "$root"
allowlist="${WINGSMITH_PROSE_ALLOWLIST:-$root/scripts/prose-allowlist.txt}"
denylist="${WINGSMITH_PROSE_DENYLIST:-$HOME/.config/wingsmith-oss/prose-denylist}"

# Word edges are spelled out: BSD and GNU grep disagree on \b and -w.
edge='[^[:alnum:]_]'
# A calendar date, not a timestamp layout or an RFC 3339 example value.
date='(^|[^0-9])20[0-9][0-9]-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])([^0-9T]|$)'
# What production did, or how the comment itself changed. Matched lowercased.
production='(queried|measured|verified|observed|seen|tested|benchmarked|reproduced) (against|in|on) prod(uction)?([^[:alnum:]_]|$)|what changed (from|since)'
# Each pattern is written so that it does not match its own spelling here.
emdash="$(printf '\342\200\224')"
tics="rather[[:space:]]than|deliberate[l]y|on[[:space:]]purpose|(^|$edge)hones[t]|the[[:space:]]whole[[:space:]]point|load-bearin[g]"

source_files='\.(go|py|ts|tsx|js|mjs|sh|bash|ya?ml|tpl)$|(^|/)(Dockerfile|Makefile|justfile)$'
test_files='_test\.(go|py|sh)$|(^|/)(tests?|testdata|__tests__)/|(^|/)(test_[^/]*|conftest)\.py$|\.(test|spec)\.[jt]sx?$'
# Lockfiles, and output generated from text that is checked where it is written.
generated='(^|/)(pnpm-lock\.yaml|uv\.lock|go\.sum)$|^libs/operator/deploy/crd/|^libs/chart/cluster-setup/crds/'

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The added lines of a diff as `path:line:text`.
added_lines() {
  awk '
    /^\+\+\+ / { path = substr($0, 7); next }
    /^@@ / { split($3, a, ","); line = substr(a[1], 2) + 0; next }
    /^\+/ { print path ":" line ":" substr($0, 2); line++ }
  '
}

diff_args=(-U0 --no-color --no-ext-diff --src-prefix=a/ --dst-prefix=b/ --diff-filter=ACMR)
case "$mode" in
  staged)
    git diff --cached "${diff_args[@]}" | added_lines >"$work/lines"
    git diff --cached --name-only --diff-filter=ACMR >"$work/paths"
    ;;
  range)
    if [ $# -ne 3 ]; then
      echo "usage: check-prose.sh range BASE HEAD" >&2
      exit 2
    fi
    git diff "${diff_args[@]}" "$2...$3" | added_lines >"$work/lines"
    git diff --name-only --diff-filter=ACMR "$2...$3" >"$work/paths"
    ;;
  tree)
    git ls-files >"$work/paths"
    ;;
  *)
    echo "usage: check-prose.sh staged|range BASE HEAD|tree" >&2
    exit 2
    ;;
esac
# Paths as `path:0:path`, so a file named after something refused is caught as well as a line.
awk '{ print $0 ":0:" $0 }' "$work/paths" >"$work/pathlines"

# Lines matching a pattern, from the diff or from the whole tree.
scan_lines() {
  if [ "$mode" = tree ]; then
    git grep -nI "$@" || true
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

if [ -s "$denylist" ]; then
  words="$(grep -v -e '^[[:space:]]*$' -e '^#' "$denylist" | paste -sd '|' -)"
  if [ -n "$words" ]; then
    names="(^|$edge)($words)($edge|\$)"
    { scan_lines -iE -e "$names"; scan_paths -iE -e "$names"; } >"$work/hits"
    report names "a word on the private prose denylist"
  fi
else
  echo "check-prose: no denylist at $denylist; skipping the names check" >&2
fi

# Keeps the hits in a comment or docstring of non-test source whose text matches RE. Patterns reach
# awk through the environment, since -v would read their backslashes as string escapes.
in_source_comment() {
  RE="$1" SRC="$source_files" TESTS="$test_files" QUOTE="[\"'#]" awk '
    {
      i = index($0, ":"); path = substr($0, 1, i - 1)
      rest = substr($0, i + 1); text = substr(rest, index(rest, ":") + 1)
    }
    path !~ ENVIRON["SRC"] || path ~ ENVIRON["TESTS"] { next }
    !match(tolower(text), ENVIRON["RE"]) { next }
    {
      before = substr(text, 1, RSTART)
      comment = before ~ /(\/\/|#|\/\*)/ || before ~ /^[[:space:]]*\*/
      docstring = path ~ /\.py$/ && (before !~ ENVIRON["QUOTE"] || before ~ /^[[:space:]]*[rbuRBU]?("""|'"'''"')/)
      if (comment || docstring) print
    }
  '
}

{
  scan_lines -E -e "$date" | in_source_comment "$date"
  scan_lines -iE -e "$production" | in_source_comment "$production"
} >"$work/hits"
report history "a date or a production fact in a source comment; keep the invariant, put the history in the commit"

# An em dash standing alone, in quotes, as an element's only text or on a line of its own, is a UI
# placeholder for an empty value, not punctuation.
{ scan_lines -iE -e "$tics"; scan_lines -F -e "$emdash"; } |
  GEN="$generated" DASH="$emdash" LONE="[\"'\`>]${emdash}[\"'\`<]" TICS="$tics" awk '
    {
      i = index($0, ":"); path = substr($0, 1, i - 1)
      rest = substr($0, i + 1); text = substr(rest, index(rest, ":") + 1)
    }
    path ~ ENVIRON["GEN"] || seen[$0]++ { next }
    {
      bare = text
      gsub(ENVIRON["LONE"], "", bare)
      if (bare ~ ("^[[:space:]]*" ENVIRON["DASH"] "[[:space:]]*$")) bare = ""
      if (index(bare, ENVIRON["DASH"]) || tolower(text) ~ ENVIRON["TICS"]) print
    }
  ' >"$work/hits"
report tics "em dash or stock phrase; say it plainly"

exit "$failed"
