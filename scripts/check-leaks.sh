#!/usr/bin/env bash
# Fails if content matches a credential pattern or the private denylist.
#
#   check-leaks.sh staged     lines added in the index (pre-commit)
#   check-leaks.sh message F  a commit message file (commit-msg)
#   check-leaks.sh tree       every tracked file (pre-commit and CI)
#
# The denylist names things that must never appear here, so it cannot live here either.
# It is read from $WINGSMITH_DENYLIST, default ~/.config/wingsmith-oss/denylist, one
# case-insensitive extended regex per line. A missing denylist fails closed.
set -euo pipefail

mode="${1:-staged}"
denylist="${WINGSMITH_DENYLIST:-$HOME/.config/wingsmith-oss/denylist}"

if [ ! -s "$denylist" ]; then
  echo "check-leaks: denylist not found at $denylist" >&2
  exit 1
fi

patterns="$(mktemp)"
trap 'rm -f "$patterns"' EXIT
grep -v -e '^\s*$' -e '^#' "$denylist" >"$patterns"
cat >>"$patterns" <<'EOF'
AKIA[0-9A-Z]{16}
ghp_[A-Za-z0-9]{30,}
github_pat_[A-Za-z0-9_]{30,}
hf_[A-Za-z0-9]{30,}
sk-[A-Za-z0-9_-]{30,}
xox[abpr]-[A-Za-z0-9-]{10,}
BEGIN [A-Z ]*PRIVATE KEY
ws[a-z]1\.[A-Za-z0-9_.:-]{20,}
EOF

case "$mode" in
  staged)
    hits="$(
      {
        git diff --cached --name-only --diff-filter=ACMR
        git diff --cached -U0 --no-color --diff-filter=ACMR | grep '^+' | grep -v '^+++' || true
      } | grep -nEi -f "$patterns" || true
    )"
    ;;
  message)
    hits="$(grep -nEi -f "$patterns" "$2" || true)"
    ;;
  tree)
    hits="$(
      {
        git ls-files
        git grep -nIiE -f "$patterns" || true
      } | grep -Ei -f "$patterns" || true
    )"
    ;;
  *)
    echo "usage: check-leaks.sh staged|message <file>|tree" >&2
    exit 2
    ;;
esac

if [ -n "$hits" ]; then
  echo "check-leaks: blocked, matches below" >&2
  echo "$hits" >&2
  exit 1
fi
