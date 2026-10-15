set shell := ["bash", "-euo", "pipefail", "-c"]

_plane    := "libs/plane"
_operator := "libs/operator"
_chart    := "libs/chart/cluster-setup"
_console  := "libs/console"
_cli      := "libs/cli"
_gateway  := "libs/gateway"
_agent    := "libs/agent"
_tunnel   := "libs/packages/tunnel"
_bench    := "libs/benchmarking"
_transit  := "libs/transit"
_conform  := "libs/conformance"
# The Docker build context for the gateway and the agent: each has a `replace` pointing at
# ../packages/tunnel, and a narrower context cannot see it.
_libs     := "libs"

_go_mods  := _operator + " " + _cli + " " + _gateway + " " + _agent + " " + _tunnel + " " + _transit
# libs/benchmarking is absent on purpose: it has no dependencies and runs on the system python3.
_py_mods  := _plane + " " + _conform

_kind_config := "dev/kind-cluster.yaml"
_kind_name   := "hangar"

# Where images and the chart are published. Override with HANGAR_REGISTRY / HANGAR_CHART_OCI to
# publish somewhere of your own; the chart's image defaults name the first.
registry  := env_var_or_default("HANGAR_REGISTRY", "ghcr.io/hangar-dev")
chart_oci := env_var_or_default("HANGAR_CHART_OCI", "oci://" + registry + "/charts")
# Clusters are amd64 and a laptop builds arm64 by default, which pushes cleanly and then
# crash-loops on `exec format error` somewhere that does not mention architecture.
platform  := env_var_or_default("HANGAR_PLATFORM", "linux/amd64")

# Show available commands
default:
    @just --list --unsorted

# ── Setup ─────────────────────────────────────────────────────────────────────

# Install every lib's dependencies: console packages, Python virtualenvs, Go modules
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    for tool in uv pnpm go; do
      command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool is not installed" >&2; exit 1; }
    done
    echo "── {{_console}}"
    (cd {{_console}} && pnpm install --frozen-lockfile)
    for d in {{_py_mods}}; do
      echo "── $d"
      # --locked where a lockfile is committed, so a fresh clone gets what CI tested.
      if [ -f "$d/uv.lock" ]; then (cd "$d" && uv sync --locked); else (cd "$d" && uv sync); fi
    done
    for d in {{_go_mods}}; do
      echo "── $d"
      (cd "$d" && go mod download)
    done
    echo "done. 'just dev' starts the stack."

# ── Dev environment ───────────────────────────────────────────────────────────

# Start the plane (Postgres, migrations, api, hub) in the background
dev-up:
    docker compose up -d --build --wait

# Stop the plane, keeping the data volume
dev-down:
    docker compose down

# Follow the api and hub logs
logs *args:
    docker compose logs -f {{ if args == "" { "api hub" } else { args } }}

# Run the whole stack locally: Postgres, the api and the hub in compose, the console beside them
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d {{_console}}/node_modules ]; then
      echo "error: console dependencies are not installed — run 'just setup' first" >&2
      exit 1
    fi
    docker compose up -d --build --wait
    (cd {{_console}} && pnpm dev)

# Create the tenants every other flow starts from, over the API  [--demo for a worked world on the fake fleet]
seed *args:
    #!/usr/bin/env bash
    set -euo pipefail
    (cd {{_plane}} && uv run python ../../dev/seed.py {{args}})

# Drop every table, migrate from scratch, and seed
reseed:
    #!/usr/bin/env bash
    set -euo pipefail
    docker compose up -d --wait db
    docker compose exec -T db psql -U hangar -d hangar -p 5442 -c 'drop schema public cascade; create schema public;'
    docker compose run --rm migrate
    docker compose up -d --wait api hub
    (cd {{_plane}} && uv run python ../../dev/seed.py)

# Run the fake operators: three clusters of hardware waiting to be enrolled  [--register to skip the screen]
fleet *args:
    #!/usr/bin/env bash
    set -euo pipefail
    (cd {{_plane}} && uv run python ../../dev/fleet.py {{args}})

# Re-snapshot the vLLM recipe corpus the plane reads  [pass a tag or sha to pin one]
vllm-recipes *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # PyYAML is the corpus's format and nobody else's: the plane parses the JSON this writes,
    # so the dependency belongs to this command rather than to the package.
    (cd {{_plane}} && uv run --with pyyaml python ../../dev/refresh_vllm_recipes.py {{args}})

# Re-snapshot NVIDIA's Dynamo deployment recipes  [pass a tag or sha to pin one]
dynamo-recipes *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # The flags of the program the cluster actually starts, which is not `vllm serve`.
    (cd {{_plane}} && uv run --with pyyaml python ../../dev/refresh_dynamo_recipes.py {{args}})

# ── Local cluster ─────────────────────────────────────────────────────────────

# Create the local kind cluster used to test libs/operator
cluster-up:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v kind >/dev/null 2>&1; then
      echo "error: kind is not installed — see https://kind.sigs.k8s.io" >&2
      exit 1
    fi
    if kind get clusters 2>/dev/null | grep -qx "{{_kind_name}}"; then
      echo "cluster '{{_kind_name}}' already exists"
    else
      kind create cluster --config {{_kind_config}}
    fi
    kubectl cluster-info --context kind-{{_kind_name}}

# Delete the local kind cluster
cluster-down:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v kind >/dev/null 2>&1; then
      echo "error: kind is not installed — nothing to delete" >&2
      exit 1
    fi
    if kind get clusters 2>/dev/null | grep -qx "{{_kind_name}}"; then
      kind delete cluster --name {{_kind_name}}
    else
      echo "cluster '{{_kind_name}}' does not exist"
    fi

# ── Format / Lint / Test ──────────────────────────────────────────────────────

# Format every lib  [--check to verify without writing]
fmt *args:
    #!/usr/bin/env bash
    set -euo pipefail
    check=""
    for a in {{args}}; do
      case "$a" in
        --check) check=1 ;;
        *) echo "error: unknown argument '$a' — use: --check" >&2; exit 1 ;;
      esac
    done

    if [ -n "$check" ]; then
      (cd {{_plane}} && uv run ruff format --check . && uv run ruff check --select I .)
    else
      (cd {{_plane}} && uv run ruff check --select I --fix . && uv run ruff format .)
    fi

    for d in {{_go_mods}}; do
      if [ -n "$check" ]; then
        out=$(cd "$d" && gofmt -l .)
        if [ -n "$out" ]; then echo "$d: not gofmt'd:"; echo "$out"; exit 1; fi
      else
        (cd "$d" && gofmt -w .)
      fi
    done

    if [ -n "$check" ]; then
      (cd {{_console}} && pnpm run format:check)
    else
      (cd {{_console}} && pnpm run format)
    fi

# Lint every lib: ruff, go vet, eslint, tsc
lint:
    #!/usr/bin/env bash
    set -euo pipefail
    (cd {{_plane}} && uv run ruff check .)
    for d in {{_go_mods}}; do
      (cd "$d" && go vet ./...)
    done
    (cd {{_console}} && pnpm run lint && pnpm run typecheck)

# Run every lib's default test suite: pytest, go test, vitest  [no database, no cluster]
test:
    #!/usr/bin/env bash
    set -euo pipefail
    (cd {{_plane}} && uv run pytest)
    for d in {{_go_mods}}; do
      (cd "$d" && go test ./...)
    done
    (cd {{_console}} && pnpm run test)
    # The runtime patches ship inside a vendor image, so they have no project to be a test target
    # of. The video geometry is stdlib-only on purpose and runs anywhere; the rest of that patch is
    # only checkable inside the image.
    python3 hangar-runtime/worker/vllm/kimi-k3-video/tests/test_geometry.py

# Run the database-backed tests against the Postgres in compose.yml
test-db *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -z "${HANGAR_TEST_DATABASE_URL:-}" ] && command -v docker >/dev/null 2>&1; then
      docker compose up -d --wait db
      export HANGAR_TEST_DATABASE_URL=postgresql+psycopg://hangar:hangar@localhost:5442/hangar
    fi
    (cd {{_plane}} && uv run pytest -m database {{args}})

# The plane figure folds the database-backed suite into the default one, so it reflects what CI
# actually runs across both jobs rather than only the markerless tests. The db suite is skipped
# when no Postgres is reachable (no HANGAR_TEST_DATABASE_URL and no docker) — the number is then
# an undercount, and that is called out rather than hidden.

# Measure test coverage for every lib, printing a per-lib total  [--html for browsable reports]
cov *args:
    #!/usr/bin/env bash
    set -euo pipefail
    html=""
    for a in {{args}}; do
      case "$a" in
        --html) html=1 ;;
        *) echo "error: unknown argument '$a' — use: --html" >&2; exit 1 ;;
      esac
    done

    marker="not database"
    if [ -n "${HANGAR_TEST_DATABASE_URL:-}" ]; then
      marker=""
    elif command -v docker >/dev/null 2>&1; then
      docker compose up -d --wait db
      export HANGAR_TEST_DATABASE_URL=postgresql+psycopg://hangar:hangar@localhost:5442/hangar
      marker=""
    else
      echo "warn: no Postgres — plane coverage omits the database-backed suite (undercount)" >&2
    fi
    report=(--cov-report=term-missing --cov-report=xml)
    [ -n "$html" ] && report+=(--cov-report=html:coverage_html)
    (cd {{_plane}} && uv run pytest -m "$marker" --cov "${report[@]}")

    for d in {{_go_mods}}; do
      (cd "$d" && go test -covermode=atomic -coverprofile=coverage.out ./...
       echo -n "$d total: "; go tool cover -func=coverage.out | tail -1 | awk '{print $NF}'
       [ -n "$html" ] && go tool cover -html=coverage.out -o coverage.html || true)
    done

    (cd {{_console}} && pnpm run test:coverage)

# ── Interop ───────────────────────────────────────────────────────────────────

# Run the cross-language interop gate: the real Go client against the real Python hub
interop *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v uv >/dev/null 2>&1; then
      echo "error: uv is not installed — the harness spawns the Python hub with it" >&2
      exit 1
    fi
    (cd {{_plane}} && uv sync --quiet)
    (cd {{_operator}} && go test -tags interop -count=1 -timeout 900s ./internal/interop/ {{args}})

# The fake client is a model of the API and no model at all of the controllers behind it: a
# rendered workload carrying a cross-namespace owner reference is stored happily by the fake and
# deleted within a second by a real cluster. Anything whose correctness depends on what
# Kubernetes *does* to an object after it is written belongs here.
#
# Uses whatever cluster the current kubecontext names. It creates and deletes its own
# namespaces and briefly patches one node's labels and allocatable, restoring both — so point it
# at a local cluster (`just cluster-up`), never at one serving traffic.

# Run the reconciler against a real API server, with a real garbage collector
e2e *args:
    #!/usr/bin/env bash
    set -euo pipefail
    context="$(kubectl config current-context 2>/dev/null || true)"
    if [ -z "$context" ]; then
      echo "error: no current kubecontext — 'just cluster-up' for a local one" >&2
      exit 1
    fi
    echo "running against '${context}' — this patches a node and creates namespaces"
    kubectl apply -f {{_operator}}/deploy/crd/placeholder.hangar.dev_syncedresources.yaml
    kubectl apply -f {{_operator}}/deploy/crd/testing_dynamographdeployments.yaml
    (cd {{_operator}} && go test -tags e2e -count=1 -timeout 600s ./internal/reconcile/ -run E2E {{args}})

# ── Gate ──────────────────────────────────────────────────────────────────────

# Everything a change has to pass: format, lint, every suite, and the interop gate
check:
    #!/usr/bin/env bash
    set -euo pipefail
    just fmt --check
    just lint
    just test
    just test-db
    just interop

# ── Request path ──────────────────────────────────────────────────────────────

# Ramp load through the real gateway, tunnel and agent on this host  [no GPU, no cluster]
transit ramp="8,32,128" *args:
    @cd {{_transit}} && go run ./cmd/hgrtransit synthetic -repo ../.. -ramp {{ramp}} {{args}}

# The CI shape of the above: one short ramp, JSON out, fails if the path cannot carry it
transit-smoke:
    @cd {{_transit}} && go run ./cmd/hgrtransit synthetic -repo ../.. -ramp 4,16 -hold 5s -baseline=false

# Ramp against a deployed gateway. Small by default on purpose.
transit-live url model *args:
    @cd {{_transit}} && go run ./cmd/hgrtransit live -url {{url}} -model {{model}} {{args}}

# ── Benchmark from here ───────────────────────────────────────────────────────

# The same harness the plane's benchmarks module schedules in a cluster, at the same version. It is
# pulled by digest for the host architecture, never by tag: the tag is multi-arch, and on Apple
# silicon the amd64 variant starts its supervisor and then never spawns a worker — no output, no
# CPU, no exit. Docker will not swap variants for a tag it already holds.
_aiperf      := "nvcr.io/nvidia/ai-dynamo/aiperf"
_aiperf_tag  := "0.12.0"

# What `just bench` is for, and what it is not.
#
# The load generator is local, so every number carries this machine's own path to the endpoint.
# Read a run for shape and for whether the path works at all, not as the cluster's latency. For a
# number worth quoting, run the benchmark in the cluster, where the plane schedules this same
# image beside the engine.
#
# The URL is resolved inside the container, so `host.docker.internal` is how it reaches a
# port-forward on this machine. BENCH_TOKEN is sent as a bearer token when set; the gateway's
# internal listener needs none. The tokenizer is a Hugging Face repository and not the served
# model name — AIPerf defaults it to `--model`, which fails the tokenizer load before it sends a
# request. Everything after the recipe name goes to `aiperf profile` unchanged.
#
#   just bench
#   BENCH_CONCURRENCY=32 BENCH_REQUESTS=500 just bench
#   BENCH_URL=http://host.docker.internal:9000 just bench --warmup-request-count 20

# One AIPerf run from this machine against an endpoint, and an HTML report of it
bench *args:
    #!/usr/bin/env bash
    set -euo pipefail
    model="${BENCH_MODEL:-gpt-oss-120b}"
    url="${BENCH_URL:-http://host.docker.internal:8080}"
    tokenizer="${BENCH_TOKENIZER:-openai/gpt-oss-120b}"
    export BENCH_ARCH=$([ "$(uname -m)" = "x86_64" ] && echo amd64 || echo arm64)
    digest=$(docker manifest inspect {{_aiperf}}:{{_aiperf_tag}} | python3 -c '
    import json, os, sys
    manifests = json.load(sys.stdin)["manifests"]
    want = os.environ["BENCH_ARCH"]
    for m in manifests:
        platform = m.get("platform", {})
        if platform.get("architecture") == want and platform.get("os") == "linux":
            print(m["digest"])
            break
    else:
        sys.exit(f"{want}/linux is not in the aiperf manifest list")
    ')
    docker pull -q "{{_aiperf}}@$digest" >/dev/null
    out=".local/aiperf/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$out" "$HOME/.cache/huggingface"
    cmd="aiperf profile --model '$model' --url '$url'"
    cmd+=" --endpoint-type '${BENCH_ENDPOINT_TYPE:-chat}' --streaming"
    if [ -n "${BENCH_TOKEN:-}" ]; then
      cmd+=" -H \"Authorization: Bearer \$BENCH_TOKEN\""
    fi
    cmd+=" --tokenizer '$tokenizer'"
    cmd+=" --concurrency ${BENCH_CONCURRENCY:-10} --request-count ${BENCH_REQUESTS:-100}"
    cmd+=" --synthetic-input-tokens-mean ${BENCH_ISL:-2000}"
    cmd+=" --output-tokens-mean ${BENCH_OSL:-256}"
    cmd+=" --extra-inputs ignore_eos:true --ui simple --artifact-dir /artifacts {{args}}"
    docker run --rm \
      -e HF_HOME=/hf -e BENCH_TOKEN="${BENCH_TOKEN:-}" \
      -v "$HOME/.cache/huggingface:/hf" -v "$PWD/$out:/artifacts" \
      "{{_aiperf}}@$digest" "$cmd"
    just bench-report "$out"

# Render an AIPerf artifact directory as one self-contained HTML page
bench-report dir:
    #!/usr/bin/env bash
    set -euo pipefail
    report=$(python3 dev/aiperf_report.py "{{dir}}")
    echo "$report"
    if [ -z "${BENCH_NO_OPEN:-}" ] && command -v open >/dev/null 2>&1; then
      open "$report"
    fi

# A suite is four harnesses against the same load points, from a plan committed in
# libs/benchmarking/plans. `just bench` above is the quick one-harness check; this is the one that
# answers how the endpoint behaves, and whether the tools agree about it. Flags only narrow a plan:
#
#   just bench-suite smoke                        # ~10 min, safe any time of day
#   just bench-suite deep --hard                  # ~2 h, includes the saturation search
#   just bench-suite smoke --model gpt-oss-20b    # one model
#   just bench-suite deep --dry-run               # every command, nothing sent
#
# --hard is required for the saturation search, which climbs load until the SLA breaks. On a live
# endpoint that is a decision, not a default.

# Run a whole benchmark plan: four harnesses, one comparison report  [smoke | deep]
bench-suite plan="smoke" *args:
    @PYTHONPATH={{_bench}} python3 -m hgrbench run {{plan}} {{args}}

# What the suite runner can drive, and what each harness is for
bench-harnesses:
    @PYTHONPATH={{_bench}} python3 -m hgrbench harnesses

# The suite plans in this checkout, with what each one costs to run
bench-plans:
    @PYTHONPATH={{_bench}} python3 -m hgrbench plans

# Re-render a suite run's report from what is already on disk  [latest by default]
bench-suite-report run="":
    @PYTHONPATH={{_bench}} python3 -m hgrbench report {{run}}

# ── Conformance ───────────────────────────────────────────────────────────────
#
# Whether an endpoint behaves the way the wire spec says, and the way the model's original
# provider does. `just bench` asks how fast an endpoint is; this asks whether it is correct.
# Neither knows anything about the plane.
#
# A profile is committed TOML naming the endpoint and the handful of things no probe is allowed
# to know — how this vendor spells thinking mode, where it puts the cached token count. Adding a
# model is a profile, not code.
#
#   just conformance example                              # observe one endpoint
#   just conformance gateway --reference moonshot-kimi-k3 # and diff against the provider
#   just conformance example --probe tools --probe cache  # narrow it
#   just conformance example --heavy                      # include the 1000-sample probes
#
# Uses the module's own virtualenv, which `just setup` creates.

# Observe an endpoint, and optionally diff it against a reference  [profile] [--reference <profile>]
conformance profile="example" *args:
    @cd {{_conform}} && PYTHONPATH=. .venv/bin/python -m hgrconform run {{profile}} {{args}}

# Every question this knows how to ask, and which row of the standard each answers
conformance-probes:
    @cd {{_conform}} && PYTHONPATH=. .venv/bin/python -m hgrconform probes

# Re-diff a stored run without touching the endpoint again  [latest by default]
conformance-diff run="":
    @cd {{_conform}} && PYTHONPATH=. .venv/bin/python -m hgrconform diff {{run}}

# The self-check: run every probe against two honest-but-noisy stubs and fail on any difference.
# This is what catches a probe comparing something that varies between runs, which is the one
# defect that makes the whole tool report noise. Run it after writing a probe.

# Run every probe against two noisy-but-honest stubs and fail on any difference
conformance-test *args:
    @cd {{_conform}} && PYTHONPATH=. .venv/bin/python -m pytest tests/ -q {{args}}

# ── Chart ─────────────────────────────────────────────────────────────────────

# Helm's crds/ dir is a plain copy, never templated — kept in step by hand when the CRDs change.
# Copy the hangar CRDs into the chart from their source of truth in libs/operator/deploy/crd
chart-sync-crds:
    #!/usr/bin/env bash
    set -euo pipefail
    cp {{_operator}}/deploy/crd/hangar.dev_syncedresources.yaml {{_chart}}/crds/
    cp {{_operator}}/deploy/crd/hangar.dev_streamcheckpoints.yaml {{_chart}}/crds/
    echo "synced CRDs into {{_chart}}/crds"

# Subchart tarballs are not vendored; template checks for them regardless of condition, so a
# temp copy with the dependencies stripped renders the hangar-owned templates offline.
# Lint and render the chart offline, catching template errors
chart-lint:
    #!/usr/bin/env bash
    set -euo pipefail
    py=(uv run --no-project --quiet --with pyyaml python)
    # The required values are the blanks an operator must fill; lint has to supply them.
    req=(--set cluster.id=lint --set cluster.hubUrl=wss://lint/v1/hub/sync
         --set enrolmentToken.value=hgrtok_lint
         --set pools.labelKey=lint.example.com/pool --set pools.gpuValue=gpu
         --set pools.cacheValue=system
         --set modelCache.storageClass=lint-models
         # Only read with serving on, where the chart refuses a route with no auth middleware
         # and, with network policy on, an ingress controller it does not know to let in.
         --set 'serving.middlewares[0].name=lint-auth'
         --set 'networkPolicy.frontendFrom[0].namespaceSelector.matchLabels.kubernetes\.io/metadata\.name=traefik')
    helm lint {{_chart}} "${req[@]}"
    work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
    cp -r {{_chart}} "$work/chart"
    "${py[@]}" - "$work/chart/Chart.yaml" <<'PY'
    import sys, pathlib
    p = pathlib.Path(sys.argv[1]); p.write_text(p.read_text().split("dependencies:")[0])
    PY
    helm template lint "$work/chart" -n hangar-system "${req[@]}" \
      --set hfToken.create=true --set hfToken.value=hf_lint \
      --set serving.enabled=true --set serving.host=lint.example.com \
      --set serving.clusterIssuer=letsencrypt-dns --set serving.backend.service=frontend >/dev/null
    # The frontend and the engine are configured separately, and either can pull a private runtime
    # image. In both cases the credential must be copied from the release namespace into the
    # workload namespace. The key is `frontend.imagePullSecrets` and not `operator.frontend.*`:
    # the pull secret is common to both frontend modes, so it stayed at `frontend.*` when the
    # per-model knobs moved under `frontend.perModel.*`.
    pull=(--set imagePullSecret.create=true --set imagePullSecret.name=ghcr-pull
          --set imagePullSecret.username=lint --set imagePullSecret.token=lint)
    helm template lint "$work/chart" -n hangar-system "${req[@]}" "${pull[@]}" \
      --set 'frontend.imagePullSecrets[0]=ghcr-pull' \
      --show-only templates/20-secrets.yaml >"$work/frontend-secrets.yaml"
    helm template lint "$work/chart" -n hangar-system "${req[@]}" "${pull[@]}" \
      --set 'operator.engine.imagePullSecrets[0]=ghcr-pull' \
      --show-only templates/20-secrets.yaml >"$work/operator-engine-secrets.yaml"
    "${py[@]}" - "$work/frontend-secrets.yaml" "$work/operator-engine-secrets.yaml" <<'PY'
    import pathlib, sys, yaml
    for path in sys.argv[1:]:
        documents = yaml.safe_load_all(pathlib.Path(path).read_text())
        if not any(
            document.get("kind") == "Secret"
            and document.get("metadata", {}).get("name") == "ghcr-pull"
            and document.get("metadata", {}).get("namespace") == "dynamo"
            for document in documents
        ):
            sys.exit(f"operator pull secret was not copied into dynamo for {path}")
    PY
    # Monitoring is off by default, so the default render above proves nothing about it. Its own
    # blanks are required only when it is on, hence a second pass rather than more entries in req.
    # The credential is not among them: the agent authenticates with the enrolment token in req.
    mon=(--set monitoring.enabled=true --set cluster.region=lint-region-1
         --set monitoring.remoteWrite.url=https://metrics.example.com/api/v1/write)
    helm template lint "$work/chart" -n hangar-system "${req[@]}" "${mon[@]}" >/dev/null
    # The escape hatch for a store that is not hangar's still needs its own pair.
    own=(--set monitoring.remoteWrite.basicAuth.useEnrolmentToken=false
         --set monitoring.remoteWrite.basicAuth.username=lint
         --set monitoring.remoteWrite.basicAuth.password=lint)
    helm template lint "$work/chart" -n hangar-system "${req[@]}" "${mon[@]}" "${own[@]}" >/dev/null
    # Off means nothing rendered, not an agent pointed nowhere.
    if helm template lint "$work/chart" -n hangar-system "${req[@]}" | grep -q '^  name: hangar-metrics-agent$'; then
      echo "the metrics agent renders with monitoring.enabled=false" >&2
      exit 1
    fi
    # The exporter must select GPU nodes. Its toleration only permits the GPU pool; with no
    # selector the DaemonSet also lands on CPU nodes, where the exporter has no GPU to read and the
    # operator discovers a target set that does not match the hardware. That reads as missing
    # devices and phantom fell_off_bus events, so it is asserted rather than left to whoever edits
    # values next. Asserted against values.yaml rather than rendered output: the render above drops
    # `dependencies:`, so nothing dcgm-exporter owns ever appears in it.
    "${py[@]}" - {{_chart}}/values.yaml <<'PY'
    import sys, pathlib, yaml
    values = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())
    selector = (values.get("dcgm-exporter") or {}).get("nodeSelector") or {}
    if not selector:
        sys.exit("dcgm-exporter has no nodeSelector; it would run on CPU nodes too")
    PY
    # Each required blank must fail loudly when left empty, not render something broken.
    for blank in pools.labelKey pools.gpuValue pools.cacheValue modelCache.storageClass enrolmentToken.value; do
      if helm template lint "$work/chart" -n hangar-system "${req[@]}" --set "$blank=" >/dev/null 2>&1; then
        echo "chart renders with an empty $blank; it should have failed" >&2
        exit 1
      fi
    done
    for blank in cluster.region monitoring.remoteWrite.url; do
      if helm template lint "$work/chart" -n hangar-system "${req[@]}" "${mon[@]}" --set "$blank=" >/dev/null 2>&1; then
        echo "chart renders with monitoring on and an empty $blank; it should have failed" >&2
        exit 1
      fi
    done
    for blank in monitoring.remoteWrite.basicAuth.username monitoring.remoteWrite.basicAuth.password; do
      if helm template lint "$work/chart" -n hangar-system "${req[@]}" "${mon[@]}" "${own[@]}" \
           --set "$blank=" >/dev/null 2>&1; then
        echo "chart renders with useEnrolmentToken=false and an empty $blank; it should have failed" >&2
        exit 1
      fi
    done
    # The enrolment token IS the remote-write credential, so an agent without one 401s forever and
    # says so nowhere but its own log. Refused at render instead.
    if helm template lint "$work/chart" -n hangar-system "${req[@]}" "${mon[@]}" \
         --set enrolmentToken.value= >/dev/null 2>"$work/err"; then
      echo "chart renders monitoring with useEnrolmentToken=true and no enrolment token" >&2
      exit 1
    fi
    if ! grep -q useEnrolmentToken "$work/err"; then
      echo "the refusal above does not say the token is the remote-write credential" >&2
      exit 1
    fi
    # A pre-created basicAuth Secret alongside useEnrolmentToken would be mounted by nothing.
    if helm template lint "$work/chart" -n hangar-system "${req[@]}" "${mon[@]}" \
         --set monitoring.remoteWrite.basicAuth.existing=true >/dev/null 2>&1; then
      echo "chart renders with both basicAuth.useEnrolmentToken and basicAuth.existing" >&2
      exit 1
    fi
    # Two scrapers for one exporter is a doubled series, so the pair is refused rather than merged.
    if helm template lint "$work/chart" -n hangar-system "${req[@]}" "${mon[@]}" \
         --set dcgm-exporter.serviceMonitor.enabled=true >/dev/null 2>&1; then
      echo "chart renders with both monitoring.enabled and dcgm-exporter.serviceMonitor.enabled" >&2
      exit 1
    fi
    echo "chart renders, and refuses each empty required value"
    uv run --no-project --quiet --with pyyaml --with pytest pytest {{_chart}}/tests -q -p no:cacheprovider

# Subcharts (dcgm-exporter, dynamo-platform) pull anonymously; only the push needs a login
# (`helm registry login`). Chart.yaml's version is the release; a tag overrides appVersion, which
# names the operator image.
# Package the chart and push it to the OCI registry  [tag]
chart-push tag="":
    #!/usr/bin/env bash
    set -euo pipefail
    just chart-sync-crds
    just chart-lint
    work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
    helm dependency build {{_chart}}
    args=()
    [ -n "{{tag}}" ] && args+=(--app-version "{{tag}}")
    helm package {{_chart}} "${args[@]}" -d "$work"
    helm push "$work"/cluster-setup-*.tgz {{chart_oci}}
    echo "pushed chart to {{chart_oci}}/cluster-setup"

# ── Images ────────────────────────────────────────────────────────────────────

# The tag every image agrees on by default: the commit being built
_release-tag:
    @git rev-parse --short=7 HEAD

# `dockerfile` is relative to `context`, which is not always the module's own directory: the
# gateway and the agent share `libs/packages/tunnel` through a `replace`, so both build from
# `libs/`, and a narrower context fails at `go mod download`.
# Build a component image  [plane|operator|gateway|agent|evalscope] [tag]
build target tag="":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{target}}" in
      plane)     context={{_libs}};     dockerfile=plane/Dockerfile ;;
      gateway)   context={{_libs}};     dockerfile=gateway/Dockerfile ;;
      operator)  context={{_operator}}; dockerfile=Dockerfile ;;
      agent)     context={{_libs}};     dockerfile=agent/Dockerfile ;;
      evalscope) context={{_bench}}/images/evalscope; dockerfile=Dockerfile ;;
      *) echo "error: unknown target {{target}} — expected plane, operator, gateway, agent or evalscope" >&2; exit 1 ;;
    esac
    tag="{{tag}}"
    [ -n "$tag" ] || tag="$(just _release-tag)"
    base="{{registry}}/{{target}}"
    echo "building ${base}:${tag} ({{platform}}) from ${context}"
    docker build --platform {{platform}} \
      --build-arg VERSION="${tag}" \
      -f "${context}/${dockerfile}" -t "${base}:${tag}" -t "${base}:latest" "${context}"

# Build a component image and push it, with :latest beside the tag  [target] [tag]
push target tag="":
    #!/usr/bin/env bash
    set -euo pipefail
    tag="{{tag}}"
    [ -n "$tag" ] || tag="$(just _release-tag)"
    just build {{target}} "$tag"
    docker push "{{registry}}/{{target}}:${tag}"
    docker push "{{registry}}/{{target}}:latest"
    echo "pushed {{registry}}/{{target}}:${tag} and :latest"
