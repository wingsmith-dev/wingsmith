#!/usr/bin/env bash
# Runs check-dco.sh against a throwaway repository.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

failures=0
fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

cd "$work"
git init -q -b main
git config user.email dev@example.com
git config user.name Dev
git config commit.gpgsign false
git config core.hooksPath /dev/null
git commit -q --allow-empty -m base
base="$(git rev-parse HEAD)"

commit() { git commit -q --allow-empty "$@"; }

expect() {
  local want="$1" name="$2" got
  if "$here/check-dco.sh" "$base" HEAD >/dev/null 2>"$work/err"; then got=pass; else got=refuse; fi
  [ "$got" = "$want" ] || fail "$name: wanted $want, got $got: $(cat "$work/err")"
}

expect pass "an empty range"

commit -s -m "signed by the author"
expect pass "a commit signed off by its author"

git config user.email Dev@Example.com
commit -s -m "signed with the email in another case"
git config user.email dev@example.com
expect pass "a sign-off whose email differs only in case"

commit -m "unsigned"
expect refuse "an unsigned commit"
git reset -q --hard HEAD~1

commit -m "signed by somebody else" -m "Signed-off-by: Other <other@example.com>"
expect refuse "a sign-off naming someone other than the author"
git reset -q --hard HEAD~1

commit -m "mentions it in the body" -m "We should add Signed-off-by: Dev <dev@example.com> later, says the text."
expect refuse "a sign-off in the prose of the body, not in a trailer"
git reset -q --hard HEAD~1

git checkout -q -b side
commit -s -m "signed on a branch"
git checkout -q main
git merge -q --no-ff --no-edit side
expect pass "a merge commit without a sign-off of its own"

if [ "$failures" -gt 0 ]; then
  echo "$failures check-dco case(s) failed" >&2
  exit 1
fi
echo "check-dco: all cases pass"
