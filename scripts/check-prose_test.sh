#!/usr/bin/env bash
# Runs check-prose.sh against throwaway repositories and checks what it refuses and what it lets by.
# The refused words are assembled at run time so this file does not trip the guard it tests.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

failures=0
fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

cluster="e""u1"
codename="ink""ling"
assistant="Claude"" Code"
runtime="libs/run""time"
day="2026-09-""01"

# A fresh repository holding the guard, its allowlist and one file, staged.
repo() {
  local dir="$work/$1" path="$2" content="$3"
  rm -rf "$dir"
  mkdir -p "$dir/scripts" "$dir/$(dirname "$path")"
  git -C "$dir" init -q
  git -C "$dir" config user.email test@example.com
  git -C "$dir" config user.name test
  cp "$here/check-prose.sh" "$dir/scripts/"
  : >"$dir/scripts/prose-allowlist.txt"
  printf '%s\n' "$content" >"$dir/$path"
  git -C "$dir" add -A
}

# expect <refuse|pass> <mode> <name> <path> <content>
expect() {
  local want="$1" mode="$2" name="$3" path="$4" content="$5" got
  repo "$name" "$path" "$content"
  if (cd "$work/$name" && scripts/check-prose.sh "$mode" >/dev/null 2>"$work/$name.err"); then
    got=pass
  else
    got=refuse
  fi
  [ "$got" = "$want" ] || fail "$name ($mode): wanted $want, got $got: $(cat "$work/$name.err")"
}

for mode in staged tree; do
  expect refuse "$mode" cluster-in-comment libs/x/a.go "// seen on $cluster under load"
  expect refuse "$mode" cluster-uppercase docs/a.md "The E""U1 cluster"
  expect pass "$mode" cluster-inside-word libs/x/a.go "// region-${cluster}x and x${cluster} are other words"
  expect refuse "$mode" codename libs/x/a.py "MODEL = '$codename'"
  expect refuse "$mode" cluster-in-path "libs/$cluster/a.go" "package a"
  expect refuse "$mode" assistant libs/x/a.ts "// written with $assistant"
  expect refuse "$mode" runtime README.md "See $runtime for the patches."
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
  expect pass "$mode" clean libs/x/a.go "// nothing to see"
done

# The allowlist drops a hit that matches it and keeps one that does not.
repo allowlisted libs/x/a.go "// seen on $cluster"
printf '%s\n' "^libs/x/a\\.go:1:// seen on" >"$work/allowlisted/scripts/prose-allowlist.txt"
(cd "$work/allowlisted" && scripts/check-prose.sh tree >/dev/null 2>&1) || fail "allowlisted hit still refused"
printf '%s\n' "^libs/x/b\\.go:" >"$work/allowlisted/scripts/prose-allowlist.txt"
if (cd "$work/allowlisted" && scripts/check-prose.sh tree >/dev/null 2>&1); then
  fail "an allowlist entry for another file let the hit by"
fi

# staged looks only at what is being added: a hit already committed does not block an unrelated change.
repo committed libs/x/a.go "// seen on $cluster"
git -C "$work/committed" commit -qm base
printf '%s\n' "// unrelated" >"$work/committed/libs/x/b.go"
git -C "$work/committed" add libs/x/b.go
(cd "$work/committed" && scripts/check-prose.sh staged >/dev/null 2>&1) || fail "staged refused a hit it did not add"
if (cd "$work/committed" && scripts/check-prose.sh tree >/dev/null 2>&1); then
  fail "tree let a committed hit by"
fi

# staged reports the line number the hit has in the new file.
repo lineno libs/x/a.go "$(printf 'package x\n\n// seen on %s' "$cluster")"
(cd "$work/lineno" && scripts/check-prose.sh staged >/dev/null 2>"$work/lineno.err") || true
grep -q "libs/x/a.go:3:" "$work/lineno.err" || fail "staged reported the wrong line: $(cat "$work/lineno.err")"

if [ "$failures" -gt 0 ]; then
  echo "$failures check-prose case(s) failed" >&2
  exit 1
fi
echo "check-prose: all cases pass"
