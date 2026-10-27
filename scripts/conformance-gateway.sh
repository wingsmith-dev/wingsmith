#!/usr/bin/env bash
# Runs hgrconform against the real gateway, built from this checkout, and diffs it against the
# engine it fronts.
#
#   conformance-gateway.sh [results-dir]     default: a temporary directory, removed afterwards
#
# Two copies of libs/conformance/tests/stub.py stand in for the engine. One sits behind the path
# `hgrtransit path` holds open (fake plane, gateway, tunnel, agent), with OPEN=1 because the gateway
# checks the caller's key and does not forward it. The other is asked directly, as the reference.
# Both are deterministic (QUIRK=none), so every difference between the two runs is something the
# gateway changed.
#
# It fails when a probe errors on the gateway, when an error body or header names a leak needle,
# or when a difference turns up that libs/conformance/tests/gateway-differences.txt does not
# accept. Each accepted difference is listed there with the reason it is expected.
#
# Needs go and uv on PATH, and libs/conformance synced (`uv sync --locked` there).
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
conform="$root/libs/conformance"
python="$conform/.venv/bin/python"
accepted="$conform/tests/gateway-differences.txt"

work="$(mktemp -d)"
results="${1:-$work/results}"
pids=()
cleanup() {
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

free_port() {
  "$python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

wait_for() {
  local what="$1" check="$2"
  for _ in $(seq 1 "${3:-60}"); do
    if eval "$check"; then return 0; fi
    sleep 1
  done
  echo "conformance-gateway: $what never came up" >&2
  return 1
}

behind="$(free_port)"
direct="$(free_port)"
gateway="$(free_port)"
export HANGAR_API_KEY="hgrconform-ci-key"

OPEN=1 QUIRK=none "$python" "$conform/tests/stub.py" "$behind" >"$work/stub-behind.log" 2>&1 &
pids+=($!)
QUIRK=none "$python" "$conform/tests/stub.py" "$direct" >"$work/stub-direct.log" 2>&1 &
pids+=($!)
for port in "$behind" "$direct"; do
  wait_for "the stub on $port" "curl -fsS -o /dev/null http://127.0.0.1:$port/v1/models" 20
done

# Built first and run directly, so the pid killed on exit is hgrtransit itself, which takes the
# gateway and agent down with it. Building those two is most of the wait.
(cd "$root/libs/transit" && go build -o "$work/hgrtransit" ./cmd/hgrtransit)
"$work/hgrtransit" path -repo "$root" -frontend "http://127.0.0.1:$behind" \
  -listen "127.0.0.1:$gateway" >"$work/path.log" 2>&1 &
pids+=($!)
if ! wait_for "the gateway path" "grep -q 'path ready' '$work/path.log'" 300; then
  cat "$work/path.log" >&2
  exit 1
fi

# The profiles claim everything the stub serves, so no probe is skipped on either side.
profile() {
  cat <<EOF
name = "$1"
base_url = "http://127.0.0.1:$2"
model = "gpt-oss-120b"
token_env = "HANGAR_API_KEY"
context_window = 4096
modalities = ["text", "image", "audio", "video"]
protocols = ["chat_completions", "messages"]
leak_needles = ["hangar", "dynamo", "vllm", "transit"]

[thinking]
syntax = "chat_template"
default_on = false
efforts = ["low", "high"]
EOF
}
profile gateway "$gateway" >"$work/gateway.toml"
profile engine "$direct" >"$work/engine.toml"

# The diff's own exit code is 1 on any difference, accepted or not; the check below decides.
(cd "$conform" && PYTHONPATH=. "$python" -m hgrconform --results "$results" \
  run "$work/gateway.toml" --reference "$work/engine.toml") || true

cd "$conform"
PYTHONPATH=. "$python" - "$results" "$accepted" <<'PY'
import sys
from pathlib import Path

from hgrconform.cli import compare_run
from hgrconform.store import latest_run, read_run
from hgrconform.probes import registry

registry.load()
results, accepted_file = Path(sys.argv[1]), Path(sys.argv[2])
path = latest_run(results)
if path is None:
    sys.exit(f"conformance-gateway: no run under {results}")
run = read_run(path)

accepted = set()
for line in accepted_file.read_text().splitlines():
    line = line.split("#", 1)[0].strip()
    if line:
        accepted.add(line)

failures = []
target = run.endpoints["target"].facts
for probe_id, fact in sorted(target.items()):
    if fact.status == "error":
        failures.append(f"{probe_id} errored on the gateway: {(fact.error or '').splitlines()[0:1]}")
leak = target.get("errors.leakage")
if leak is None or leak.observed.get("leaked"):
    found = leak.observed.get("needles_found") if leak else "not run"
    failures.append(f"errors.leakage: the gateway named {found} to a caller")

comparison = compare_run(run)
seen = set()
for difference in comparison.differences:
    seen.add(difference.label)
    if difference.label not in accepted:
        failures.append(f"new difference {difference.label}: gateway {difference.target!r}, engine {difference.reference!r}")
for label in sorted(accepted - seen):
    print(f"conformance-gateway: accepted difference {label} no longer occurs; drop it from {accepted_file.name}")

if failures:
    print("\nconformance-gateway: failed", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    sys.exit(1)
print(f"conformance-gateway: {len(target)} probes ran through the gateway, nothing leaked, "
      f"{len(comparison.differences)} difference(s), all accepted")
PY
