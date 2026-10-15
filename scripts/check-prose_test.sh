#!/usr/bin/env bash
# Runs check-prose.sh against throwaway repositories and checks what it refuses and what it lets by.
# Refused phrases are assembled at run time so this file does not trip the guard it tests, and the
# names come from a made-up denylist: the real one is private.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

failures=0
fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

export HANGAR_PROSE_DENYLIST="$work/denylist"
printf '%s\n' '# a comment line' 'zq9' 'frobnitz-dev' 'libs/frob' 'Acme Assist' >"$HANGAR_PROSE_DENYLIST"

day="2026-09-""01"
dash="$(printf '\342\200\224')"
phrase="rather"" than"

# A fresh repository holding the guard, its allowlist and one file, staged on top of a first commit.
repo() {
  local dir="$work/$1" path="$2" content="$3"
  rm -rf "$dir"
  mkdir -p "$dir/scripts" "$dir/$(dirname "$path")"
  git -C "$dir" init -q
  git -C "$dir" config user.email test@example.com
  git -C "$dir" config user.name test
  git -C "$dir" config commit.gpgsign false
  git -C "$dir" config core.hooksPath /dev/null
  cp "$here/check-prose.sh" "$dir/scripts/"
  : >"$dir/scripts/prose-allowlist.txt"
  git -C "$dir" add -A
  git -C "$dir" commit -qm base
  printf '%s\n' "$content" >"$dir/$path"
  git -C "$dir" add -A
}

# Runs the guard in the repository the way `mode` is used: on the index, on the commit a pull
# request would add, or on the tree after that commit.
run() {
  local dir="$1" mode="$2"
  case "$mode" in
    staged) (cd "$dir" && scripts/check-prose.sh staged) ;;
    range)
      git -C "$dir" commit -qm head
      (cd "$dir" && scripts/check-prose.sh range HEAD~1 HEAD)
      ;;
    tree)
      git -C "$dir" commit -qm head
      (cd "$dir" && scripts/check-prose.sh tree)
      ;;
  esac
}

# expect <refuse|pass> <mode> <name> <path> <content>
expect() {
  local want="$1" mode="$2" name="$3" path="$4" content="$5" got
  repo "$name" "$path" "$content"
  if run "$work/$name" "$mode" >/dev/null 2>"$work/$name.err"; then
    got=pass
  else
    got=refuse
  fi
  [ "$got" = "$want" ] || fail "$name ($mode): wanted $want, got $got: $(cat "$work/$name.err")"
}

for mode in staged range tree; do
  expect refuse "$mode" name-in-comment libs/x/a.go "// seen on zq9 under load"
  expect refuse "$mode" name-uppercase docs/a.md "The ZQ9 cluster"
  expect pass "$mode" name-inside-word libs/x/a.go "// region-zq9x and xzq9 are other words"
  expect refuse "$mode" name-with-dash libs/x/a.py "MODEL = 'frobnitz-dev'"
  expect refuse "$mode" name-in-path libs/zq9/a.go "package a"
  expect refuse "$mode" name-with-space libs/x/a.ts "// written with Acme Assist"
  expect refuse "$mode" name-with-slash README.md "See libs/frob for the patches."
  expect refuse "$mode" date-go-comment libs/x/a.go "// measured on $day"
  expect refuse "$mode" date-yaml-comment libs/chart/values.yaml "# broke on $day"
  expect refuse "$mode" date-py-docstring libs/x/a.py "Checked against the sources on $day."
  expect refuse "$mode" date-py-comment libs/x/a.py "x = 1  # since $day"
  expect pass "$mode" date-py-literal libs/x/a.py "VERSION = \"$day\""
  expect pass "$mode" date-go-layout libs/x/a.go 'const layout = "2006-01-02T15:04:05Z"'
  expect pass "$mode" date-go-test libs/x/a_test.go "// the outage of $day, as a test"
  expect pass "$mode" date-py-tests-dir libs/x/tests/a.py "# from $day"
  expect pass "$mode" date-markdown docs/decisions/0001.md "Date: $day"
  expect pass "$mode" timestamp-example libs/x/a.go "// --from ${day}T12:00:00Z"
  expect refuse "$mode" prod-go-comment libs/x/a.go "// Queried against production: 6.68 req/s"
  expect refuse "$mode" prod-yaml-comment libs/chart/values.yaml "# verified in prod with 8 GPUs"
  expect refuse "$mode" revision-history libs/x/a.py "# WHAT CHANGED FROM THE FIRST VERSION"
  expect pass "$mode" prod-in-docs docs/a.md "Measured in production, the p99 was 40 ms."
  expect pass "$mode" prod-in-code libs/x/a.go 'env := "production"'
  expect pass "$mode" clean libs/x/a.go "// nothing to see"
done

# Tics are refused in what a change adds, and left alone in the tree while it is cleaned up.
for mode in staged range; do
  expect refuse "$mode" tic-emdash docs/a.md "One thing $dash then another."
  expect refuse "$mode" tic-phrase libs/x/a.go "// polls $phrase waits"
  expect refuse "$mode" tic-capitalised libs/x/a.py "# Hone""stly, it works"
  expect refuse "$mode" tic-on-purpose libs/x/a.ts "// left empty on"" purpose"
  expect pass "$mode" emdash-placeholder libs/x/a.tsx "const empty = '$dash'"
  expect refuse "$mode" emdash-placeholder-and-prose libs/x/a.tsx "const empty = '$dash' $dash see below"
  expect pass "$mode" emdash-in-lockfile libs/console/pnpm-lock.yaml "  note: a $dash b"
  expect pass "$mode" emdash-in-crd libs/operator/deploy/crd/a.yaml "description: a $dash b"
done
expect pass tree tic-in-tree docs/a.md "One thing $dash then another."

# Without the private denylist the names check is skipped, and says so.
repo no-denylist libs/x/a.go "// seen on zq9"
if ! (cd "$work/no-denylist" && HANGAR_PROSE_DENYLIST="$work/absent" scripts/check-prose.sh staged \
  >/dev/null 2>"$work/no-denylist.err"); then
  fail "a missing denylist refused: $(cat "$work/no-denylist.err")"
fi
grep -q 'skipping the names check' "$work/no-denylist.err" || fail "a missing denylist was not reported"

# The allowlist drops a hit that matches it and keeps one that does not.
repo allowlisted libs/x/a.go "// seen on zq9"
git -C "$work/allowlisted" commit -qm head
printf '%s\n' "^libs/x/a\\.go:1:// seen on" >"$work/allowlisted/scripts/prose-allowlist.txt"
(cd "$work/allowlisted" && scripts/check-prose.sh tree >/dev/null 2>&1) || fail "allowlisted hit still refused"
printf '%s\n' "^libs/x/b\\.go:" >"$work/allowlisted/scripts/prose-allowlist.txt"
if (cd "$work/allowlisted" && scripts/check-prose.sh tree >/dev/null 2>&1); then
  fail "an allowlist entry for another file let the hit by"
fi

# staged and range look only at what is added: a hit already committed does not block an
# unrelated change.
repo committed libs/x/a.go "// seen on zq9 $dash"
git -C "$work/committed" commit -qm old
printf '%s\n' "// unrelated" >"$work/committed/libs/x/b.go"
git -C "$work/committed" add libs/x/b.go
(cd "$work/committed" && scripts/check-prose.sh staged >/dev/null 2>&1) || fail "staged refused a hit it did not add"
git -C "$work/committed" commit -qm new
(cd "$work/committed" && scripts/check-prose.sh range HEAD~1 HEAD >/dev/null 2>&1) ||
  fail "range refused a hit it did not add"
if (cd "$work/committed" && scripts/check-prose.sh tree >/dev/null 2>&1); then
  fail "tree let a committed hit by"
fi

# staged reports the line number the hit has in the new file.
repo lineno libs/x/a.go "$(printf 'package x\n\n// seen on zq9')"
(cd "$work/lineno" && scripts/check-prose.sh staged >/dev/null 2>"$work/lineno.err") || true
grep -q "libs/x/a.go:3:" "$work/lineno.err" || fail "staged reported the wrong line: $(cat "$work/lineno.err")"

# range needs both ends.
repo usage libs/x/a.go "// fine"
if (cd "$work/usage" && scripts/check-prose.sh range HEAD >/dev/null 2>&1); then
  fail "range ran with one revision"
fi

if [ "$failures" -gt 0 ]; then
  echo "$failures check-prose case(s) failed" >&2
  exit 1
fi
echo "check-prose: all cases pass"
