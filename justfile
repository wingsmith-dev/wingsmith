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
# libs/benchmarking is absent: it has no dependencies, so it runs through
# `uv run --no-project` with nothing to install.
_py_mods  := _plane + " " + _conform
_bench_py := "PYTHONPATH=" + _bench + " uv run --no-project --python 3.12"

_kind_config := "dev/kind-cluster.yaml"
_kind_name   := "hangar"

# Shared refusals for the cluster and release recipes; see the script's header.
_guards := "dev/guards.sh"

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
      echo "error: console dependencies are not installed; run 'just setup' first" >&2
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
    # so the dependency belongs to this command, not to the package.
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
      echo "error: kind is not installed; see https://kind.sigs.k8s.io" >&2
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
      echo "error: kind is not installed; nothing to delete" >&2
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
        *) echo "error: unknown argument '$a'; use: --check" >&2; exit 1 ;;
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

# Run every lib's default test suite: pytest, go test -race, vitest  [no database, no cluster]
test:
    #!/usr/bin/env bash
    set -euo pipefail
    (cd {{_plane}} && uv run pytest)
    (cd {{_conform}} && uv run --locked python -m pytest tests -q)
    {{_bench_py}} --with pytest pytest {{_bench}}/tests -q -p no:cacheprovider
    # The dev scripts run in the plane's virtualenv, so their tests decode against its schemas.
    uv run --project {{_plane}} pytest dev/tests -q -p no:cacheprovider
    for d in {{_go_mods}}; do
      (cd "$d" && go test -race ./...)
    done
    (cd {{_console}} && pnpm run test)

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
# actually runs across both jobs, not only the markerless tests. The db suite is skipped
# when no Postgres is reachable (no HANGAR_TEST_DATABASE_URL and no docker): the number is then
# an undercount, and that is called out, not hidden.

# Measure test coverage for every lib, printing a per-lib total  [--html for browsable reports]
cov *args:
    #!/usr/bin/env bash
    set -euo pipefail
    html=""
    for a in {{args}}; do
      case "$a" in
        --html) html=1 ;;
        *) echo "error: unknown argument '$a'; use: --html" >&2; exit 1 ;;
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
      echo "warn: no Postgres; plane coverage omits the database-backed suite (undercount)" >&2
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

# The hub half of the interop harness, which the Go side launches; keep in step with `hubScript`
# in libs/operator/internal/interop/harness_test.go.
_interop_hub := "libs/operator/internal/interop/testdata/hub.py"

# Run the cross-language interop gate: the real Go client against the real Python hub
interop *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f {{_interop_hub}} ]; then
      echo "interop: skipped, {{_interop_hub}} (the hub half of the harness) is not in this checkout" >&2
      exit 0
    fi
    if ! command -v uv >/dev/null 2>&1; then
      echo "error: uv is not installed; the harness spawns the Python hub with it" >&2
      exit 1
    fi
    (cd {{_plane}} && uv sync --quiet)
    (cd {{_operator}} && go test -race -tags interop -count=1 -timeout 900s ./internal/interop/ {{args}})

# The fake client is a model of the API and no model at all of the controllers behind it: a
# rendered workload carrying a cross-namespace owner reference is stored happily by the fake and
# deleted within a second by a real cluster. Anything whose correctness depends on what
# Kubernetes *does* to an object after it is written belongs here.
#
# It creates and deletes its own namespaces and briefly patches one node's labels and
# allocatable, restoring both. So it runs against the current context only when that is a kind
# cluster (`just cluster-up`); any other context has to be named in HANGAR_E2E_CONTEXT. The Go test
# is handed a kubeconfig holding that one context, so it cannot wander onto another.
#
# The DynamoGraphDeployment CRD is a test stub. It is installed only where the real one is absent,
# and removed again when the run ends.

# Run the reconciler against a real API server, with a real garbage collector
e2e *args:
    #!/usr/bin/env bash
    set -euo pipefail
    context="$({{_guards}} e2e-context)"
    kube=(kubectl --context "$context")
    stub={{_operator}}/deploy/crd/testing_dynamographdeployments.yaml
    work="$(mktemp -d)"
    installed=""
    cleanup() {
      if [ -n "$installed" ]; then
        "${kube[@]}" delete --ignore-not-found --wait=false -f "$stub" >/dev/null || true
      fi
      rm -rf "$work"
    }
    trap cleanup EXIT
    "${kube[@]}" config view --minify --flatten >"$work/kubeconfig"
    echo "running against '${context}': this patches a node and creates namespaces"
    "${kube[@]}" apply -f {{_operator}}/deploy/crd/hangar.dev_syncedresources.yaml
    if "${kube[@]}" get crd dynamographdeployments.nvidia.com >/dev/null 2>&1; then
      echo "dynamographdeployments.nvidia.com is already installed; leaving it as it is"
    else
      "${kube[@]}" apply -f "$stub"
      installed=1
    fi
    (cd {{_operator}} && KUBECONFIG="$work/kubeconfig" go test -tags e2e -count=1 -timeout 600s ./internal/reconcile/ -run E2E {{args}})

# ── Gate ──────────────────────────────────────────────────────────────────────

# Every relative link, anchor and backticked repository path in the tracked Markdown resolves
check-docs:
    scripts/check-docs.sh

# Everything a change has to pass: format, lint, every suite, the chart, and the interop gate
check:
    #!/usr/bin/env bash
    set -euo pipefail
    just fmt --check
    just lint
    just test
    just test-db
    just chart-lint
    just interop

# ── Request path ──────────────────────────────────────────────────────────────

# Ramp load through the real gateway, tunnel and agent on this host  [no GPU, no cluster]
transit ramp="8,32,128" *args:
    @cd {{_transit}} && go run ./cmd/hgrtransit synthetic -repo ../.. -ramp {{ramp}} {{args}}

# The CI shape of the above: one short ramp, JSON out, fails if the path cannot carry it
transit-smoke:
    @cd {{_transit}} && go run ./cmd/hgrtransit synthetic -repo ../.. -ramp 4,16 -hold 5s -baseline=false -json

# Ramp against a deployed gateway. Small by default.
transit-live url model *args:
    @cd {{_transit}} && go run ./cmd/hgrtransit live -url {{url}} -model {{model}} {{args}}

# ── Benchmark from here ───────────────────────────────────────────────────────

# What `just bench` is for, and what it is not.
#
# The load generator is local, so every number carries this machine's own path to the endpoint.
# Read a run for shape and for whether the path works at all, not as the cluster's latency. For a
# number worth quoting, run the benchmark in the cluster, where the plane schedules the same AIPerf
# image beside the engine.
#
# It is one AIPerf cell run by hgrbench: dev/bench_plan.py writes the plan from the BENCH_*
# variables it documents, and hgrbench pins the image by digest, runs it, and renders the report.
# The URL is resolved inside the container, so `host.docker.internal` is how it reaches a
# port-forward on this machine. BENCH_TOKEN, when set, is passed to the container by name and
# never appears on a command line. Everything after the recipe name goes to `hgrbench run`.
#
#   BENCH_MODEL=openai/gpt-oss-20b just bench
#   BENCH_MODEL=served-name BENCH_TOKENIZER=org/repo BENCH_CONCURRENCY=32 just bench
#   BENCH_MODEL=org/model just bench --dry-run

# One AIPerf run from this machine against an endpoint, and an HTML report of it
bench *args:
    #!/usr/bin/env bash
    set -euo pipefail
    plans=".local/bench/plans"
    mkdir -p "$plans"
    plan="$plans/bench-$(date +%Y%m%d-%H%M%S).toml"
    uv run --no-project --python 3.12 python dev/bench_plan.py >"$plan"
    {{_bench_py}} python -m hgrbench run "$plan" {{args}}

# A suite is four harnesses against the same load points, from a plan that ships with hgrbench
# (`just bench-plans` lists them). `just bench` above is the quick one-harness check; this is the
# one that answers how the endpoint behaves, and whether the tools agree about it. Flags only
# narrow a plan:
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
    @{{_bench_py}} python -m hgrbench run {{plan}} {{args}}

# What the suite runner can drive, and what each harness is for
bench-harnesses:
    @{{_bench_py}} python -m hgrbench harnesses

# The suite plans in this checkout, with what each one costs to run
bench-plans:
    @{{_bench_py}} python -m hgrbench plans

# Re-render a run's report, from `bench` or `bench-suite`, from what is already on disk  [latest by default]
bench-report run="":
    @{{_bench_py}} python -m hgrbench report {{run}}

# ── Conformance ───────────────────────────────────────────────────────────────
#
# Whether an endpoint behaves the way the wire spec says, and the way the model's original
# provider does. `just bench` asks how fast an endpoint is; this asks whether it is correct.
# Neither knows anything about the plane.
#
# A profile is committed TOML naming the endpoint and the handful of things no probe is allowed
# to know: how this vendor spells thinking mode, where it puts the cached token count. Adding a
# model is a profile, not code.
#
#   just conformance example                              # observe one endpoint
#   just conformance gateway --reference <profile>        # and diff against the provider
#   just conformance example --probe tools --probe cache  # narrow it
#   just conformance example --heavy                      # include eos.suppression (50 samples)
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

# The self-check: run every probe against two correct-but-noisy stubs and fail on any difference.
# This is what catches a probe comparing something that varies between runs, which is the one
# defect that makes the whole tool report noise. Run it after writing a probe.

# Run every probe against two noisy-but-correct stubs and fail on any difference
conformance-test *args:
    @cd {{_conform}} && PYTHONPATH=. .venv/bin/python -m pytest tests/ -q {{args}}

# The probes through the real gateway, built from this checkout, against a stub engine, diffed
# against the same stub asked directly. Fails on an error, a leak, or a difference that
# libs/conformance/tests/gateway-differences.txt does not accept (scripts/conformance-gateway.sh).
# Run the probes through a locally built gateway and diff it against the engine it fronts
conformance-gateway:
    scripts/conformance-gateway.sh

# ── Chart ─────────────────────────────────────────────────────────────────────

# Helm's crds/ dir is a plain copy, never templated: kept in step by hand when the CRDs change.
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
    # blanks are required only when it is on, hence a second pass, not more entries in req.
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
    # devices and phantom fell_off_bus events, so it is asserted, not left to whoever edits
    # values next. Asserted against values.yaml, not rendered output: the render above drops
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
    # Two scrapers for one exporter is a doubled series, so the pair is refused, not merged.
    if helm template lint "$work/chart" -n hangar-system "${req[@]}" "${mon[@]}" \
         --set dcgm-exporter.serviceMonitor.enabled=true >/dev/null 2>&1; then
      echo "chart renders with both monitoring.enabled and dcgm-exporter.serviceMonitor.enabled" >&2
      exit 1
    fi
    echo "chart renders, and refuses each empty required value"
    uv run --no-project --quiet --with pyyaml --with pytest pytest {{_chart}}/tests -q -p no:cacheprovider

# Subcharts (dcgm-exporter, dynamo-platform) pull anonymously; only the push needs a login
# (`helm registry login`). Chart.yaml's version is the release, and its appVersion, the image tag
# the chart defaults to, must equal it: a release publishes images at its own version and no other
# tag, so any other appVersion installs images that do not exist.
#
# What is pushed is the committed chart, not the directory: it is packaged from `git archive`, so a
# values file kept beside it and ignored by git cannot ride along. Before that the whole tree goes
# through scripts/check-leaks.sh (which reads a denylist; see its header), the chart directory must
# match HEAD, and a version already in the registry is refused, not overwritten.
# Package the committed chart and push it to the OCI registry
chart-push:
    #!/usr/bin/env bash
    set -euo pipefail
    scripts/check-leaks.sh tree
    just chart-sync-crds
    {{_guards}} clean-tree {{_chart}}
    just chart-lint
    work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
    git archive --format=tar HEAD {{_chart}} | tar -x -C "$work"
    chart="$work/{{_chart}}"
    helm dependency build "$chart"
    helm package "$chart" -d "$work/out"
    pkg="$(ls "$work"/out/cluster-setup-*.tgz)"
    meta="$(helm show chart "$pkg")"
    version="$(awk '/^version:/ {print $2}' <<<"$meta")"
    app="$(awk '/^appVersion:/ {print $2}' <<<"$meta" | tr -d "\"'")"
    if [ "$app" != "$version" ]; then
      echo "error: Chart.yaml has appVersion '${app}' and version '${version}'; they must be equal" >&2
      exit 1
    fi
    {{_guards}} chart-unpublished "{{chart_oci}}/cluster-setup" "$version"
    helm push "$pkg" {{chart_oci}}
    echo "pushed cluster-setup ${version} (appVersion ${app}) to {{chart_oci}}"

# ── Images ────────────────────────────────────────────────────────────────────

# The tag every image agrees on by default: the commit being built. The image is built from that
# commit's tree, so the tag names exactly the source that went into it.
_release-tag:
    @git rev-parse --short=7 HEAD

# `dockerfile` is relative to `context`, which is not always the module's own directory: the
# gateway and the agent share `libs/packages/tunnel` through a `replace`, so both build from
# `libs/`, and a narrower context fails at `go mod download`.
#
# The context is HEAD's tree at `context`, piped in from `git archive`, never the directory itself:
# whatever git ignores there (local values, keys, a kubeconfig) stays out of the image. A dirty
# tree is still refused, because its edits would silently not be in what gets built.
#
# There is no `latest`. A tag is pushed once and never moved; a new build is a new tag.
# Build a component image from a clean tree  [plane|operator|gateway|agent|evalscope] [tag]
build target tag="":
    #!/usr/bin/env bash
    set -euo pipefail
    {{_guards}} clean-tree
    case "{{target}}" in
      plane)     context={{_libs}};     dockerfile=plane/Dockerfile ;;
      gateway)   context={{_libs}};     dockerfile=gateway/Dockerfile ;;
      operator)  context={{_operator}}; dockerfile=Dockerfile ;;
      agent)     context={{_libs}};     dockerfile=agent/Dockerfile ;;
      evalscope) context={{_bench}}/images/evalscope; dockerfile=Dockerfile ;;
      *) echo "error: unknown target {{target}}; expected plane, operator, gateway, agent or evalscope" >&2; exit 1 ;;
    esac
    tag="{{tag}}"
    [ -n "$tag" ] || tag="$(just _release-tag)"
    if [ "$tag" = latest ]; then
      echo "error: 'latest' is not a release tag; leave the tag out to use the commit" >&2
      exit 1
    fi
    base="{{registry}}/{{target}}"
    echo "building ${base}:${tag} ({{platform}}) from ${context} at $(git rev-parse --short=7 HEAD)"
    {{_guards}} head-context "${context}" | docker build --platform {{platform}} \
      --build-arg VERSION="${tag}" \
      -f "${dockerfile}" -t "${base}:${tag}" -

# Build a component image from a clean tree and push it under a tag the registry does not have yet  [target] [tag]
push target tag="":
    #!/usr/bin/env bash
    set -euo pipefail
    {{_guards}} clean-tree
    tag="{{tag}}"
    [ -n "$tag" ] || tag="$(just _release-tag)"
    {{_guards}} image-unpublished "{{registry}}/{{target}}:${tag}"
    just build {{target}} "$tag"
    docker push "{{registry}}/{{target}}:${tag}"
    echo "pushed {{registry}}/{{target}}:${tag}"
