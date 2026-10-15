#!/usr/bin/env bash
# Fails unless every commit in a range carries a Signed-off-by from its author (the DCO).
#
#   check-dco.sh <base> <head>    commits reachable from head and not from base
#
# The sign-off must name the commit's author email: a trailer for somebody else is a
# certification by somebody else. Merge commits are skipped, since they add no code of their own.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: check-dco.sh <base> <head>" >&2
  exit 2
fi
base="$1"
head="$2"

failed=0
checked=0
for sha in $(git rev-list --no-merges "$base..$head"); do
  checked=$((checked + 1))
  email="$(git log -1 --format='%ae' "$sha" | tr '[:upper:]' '[:lower:]')"
  signoffs="$(git log -1 --format='%(trailers:key=Signed-off-by,valueonly)' "$sha" | tr '[:upper:]' '[:lower:]')"
  if ! printf '%s\n' "$signoffs" | grep -qF "<$email>"; then
    echo "check-dco: $(git log -1 --format='%h %s' "$sha")" >&2
    echo "  no Signed-off-by for the author <$email>" >&2
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  cat >&2 <<'EOF'

Every commit needs a sign-off certifying the Developer Certificate of Origin (see CONTRIBUTING.md).
Add it to the commits on this branch with:  git rebase --signoff <base>
EOF
  exit 1
fi
echo "check-dco: $checked commit(s) signed off"
