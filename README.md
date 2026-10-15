# Hangar

A control plane for serving models across several Kubernetes GPU clusters. Hangar enrols your
clusters, keeps a per-device inventory of them, places each model on one or more clusters, renders
it there as an [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo) deployment, and routes
OpenAI-compatible requests to it through its own gateway. Tenants, endpoints, quotas and
operations such as drain and scale are managed from a web console or the `hgr` CLI.

Hangar serves models; it does not run training or batch jobs. Inside each cluster it drives NVIDIA
Dynamo (with vLLM, SGLang or TensorRT-LLM as the engine), and that is the only serving runtime it
supports today. What is planned but not built is in the [roadmap](docs/roadmap.md).

> **Status: pre-release.** Nothing is published yet: no tagged release, no images, no chart. APIs,
> CRDs and the wire protocol will change without notice. Hangar is built for one organisation
> running its own clusters, operated by a single admin or a small team sharing that role; there
> are no per-user roles or SSO.

## How it works

The control plane stores **intent** and each cluster stores **truth**. An operator runs in every
cluster, dials out to the control plane over a single WebSocket, receives Hangar's own domain
objects (never Kubernetes manifests), renders them into whatever the cluster runs, and reports what
it observes back up. Nothing ever dials into a cluster, and a cluster keeps serving when the control
plane is down. Inference traffic never passes through the control plane: the gateway, the one
Hangar process in the request path, takes OpenAI-compatible requests and forwards them over an
HTTP/2 tunnel that an agent in each cluster dials out to, using a routing table the plane derives
and the gateway caches. A running gateway keeps serving when the plane is down, but it needs the
plane to start. Cluster selection is global and lives in the plane; device selection is local and
stays in the cluster.

```
console ──http──► api ◄──► Postgres ◄──► hub ◄── operator (one per cluster, dials out)
hgr     ──http──►

caller ──► gateway ◄── tunnel ── agent (one per cluster, dials out) ──► model frontend
```

The full design, with the reasoning behind each choice and how Hangar relates to Dynamo, llm-d,
KServe and others, is in [`docs/architecture/`](docs/architecture/README.md). Individual decisions
are recorded in [`docs/decisions/`](docs/decisions/), and [`docs/glossary.md`](docs/glossary.md)
defines the terms.

## Components

| Path | Language | What it is |
|---|---|---|
| [`libs/plane`](libs/plane/README.md) | Python | The control plane: the HTTP API, the **hub** that terminates operator streams, and the **reactor** that derives alerts. One package, three processes, one Postgres. |
| [`libs/operator`](libs/operator/README.md) | Go | `hangar-operator`, one per cluster. Syncs desired state from the hub into CRDs, renders each deployment as an NVIDIA Dynamo graph, and reports observed state. |
| [`libs/gateway`](libs/gateway/README.md) | Go | The data plane. One OpenAI-compatible hop that routes on `model`, terminates the agents' tunnels, and identifies tenants. Keeps serving from its cached routing table if the plane goes down, but needs the plane to start. |
| [`libs/agent`](libs/agent/README.md) | Go | `hangar-agent`, one per cluster. Dials out to every gateway replica and proxies inference to the model's frontend inside the cluster. |
| [`libs/console`](libs/console/README.md) | TypeScript | The web console (React, TanStack Router). Talks to the plane's HTTP API only. Runs against built-in fixtures with no backend. |
| [`libs/cli`](libs/cli/README.md) | Go | `hgr`, the command line. Same API as the console, with stable JSON output and an offline `--fake` backend. |
| [`libs/chart`](libs/chart/cluster-setup/README.md) | Helm | `cluster-setup`, everything a cluster installs to be managed by Hangar: the operator, the agent, CRDs, the NVIDIA Dynamo platform, the DCGM exporter, and optional monitoring. |
| [`libs/packages/contract`](libs/packages/contract/README.md) | Markdown, JSON | The plane-operator wire protocol, the HTTP API's conventions with its generated [route reference](libs/packages/contract/REFERENCE.md) and [OpenAPI schema](libs/packages/contract/openapi.json), the device-class table, and the fixture corpus both sides test against. |
| [`libs/packages/tunnel`](libs/packages/tunnel) | Go | Both ends of the gateway-agent tunnel. Its own module because neither side owns it. |
| [`libs/benchmarking`](libs/benchmarking/README.md) | Python | `hgrbench`. Runs four load generators against an OpenAI-compatible endpoint and reports them side by side. |
| [`libs/transit`](libs/transit/README.md) | Go | `hgrtransit`. Measures the request path (gateway, tunnel, agent) on one host, with no GPU and no cluster. |
| [`libs/conformance`](libs/conformance/README.md) | Python | `hgrconform`. Probes an OpenAI-compatible endpoint and diffs its behaviour against a reference provider. |

## Quickstart

You need [`just`](https://github.com/casey/just), Docker, [`uv`](https://docs.astral.sh/uv/),
`pnpm`, Go and Python 3.12. `kind`, `kubectl` and `helm` are only needed for the cluster recipes.
`just` on its own lists every recipe.

```sh
just setup      # install console packages, Python virtualenvs and Go modules
just dev        # Postgres, migrations, api and hub in compose, plus the console dev server
just seed       # add example tenants to a fresh database  [--demo for a whole worked example]
just fleet      # three fake clusters that enrol themselves when registered in the console
```

| Process | Address |
|---|---|
| API | `http://localhost:8000` |
| Hub | `ws://localhost:8001/v1/hub/sync` |
| Console | `http://localhost:5273` |
| Postgres | `localhost:5442` (`hangar`/`hangar`) |

Tenants can also be created from the console or through the API; `just seed` only saves the
clicks. Compose runs with `HANGAR_APP_ENV=local`, which turns admin auth off and relaxes the
credentials the plane requires at boot, so a fresh checkout runs with no configuration. It is for
local development only. `just dev-up` starts the same stack without the console, `just logs`
follows it, `just dev-down` stops it, and `just reseed` drops the database and starts over.

For a real operator against a local Kubernetes cluster, `just cluster-up` creates a kind cluster from
`dev/kind-cluster.yaml`, and `libs/operator/Makefile` builds and loads the operator into it.

The quickstart stops short of serving a model. The fake fleet has no GPUs, and a kind cluster can
run the operator but not a model. Serving one needs a cluster with NVIDIA GPUs and the Dynamo
platform, installed as described below.

The CLI works offline against a built-in fake:

```sh
cd libs/cli && go build -o bin/hgr ./cmd/hgr
./bin/hgr --fake cluster ls
./bin/hgr config set context.server http://localhost:8000
```

### Checks

```sh
just fmt        # ruff, gofmt, prettier   [--check to verify without writing]
just lint       # ruff, go vet, eslint, tsc
just test       # pytest, go test, vitest; no database or cluster needed
just test-db    # the database-backed plane tests, against the Postgres in compose
just interop    # the real Go operator client against the real Python hub
just check      # all of the above, in order
```

`just chart-lint` renders the chart offline and checks that each required value is refused when
empty. `just transit`, `just bench-suite` and `just conformance` run the three measurement tools.

## Installing

No images or charts are published yet, so build them into a registry you control:

```sh
export HANGAR_REGISTRY=registry.example.com/hangar
just push plane && just push gateway && just push operator && just push agent
HANGAR_CHART_OCI=oci://registry.example.com/hangar/charts just chart-push
```

Then:

1. Run the plane and the gateway against a Postgres you operate and back up, following
   [`docs/operations/deploy-plane.md`](docs/operations/deploy-plane.md). There is no Helm chart for
   the plane yet.
2. Register each cluster in the console. Registration mints the cluster's enrolment token and
   renders its `values.yaml`.
3. Install the [`cluster-setup`](libs/chart/cluster-setup/README.md) chart in each cluster with
   those values. The cluster needs schedulable NVIDIA GPUs; the chart installs the Dynamo platform,
   the DCGM exporter, the operator and the agent.

## Repository layout

```
libs/                one directory per component, each with its own README
docs/architecture/   how the system is put together, and why
docs/decisions/      architecture decision records
docs/operations/     deploying and running Hangar
docs/roadmap.md      what is planned and not built
docs/glossary.md     the terms the docs and code use
dev/                 local-development scripts: seed data, the fake fleet, the kind config
compose.yml          the local control plane: Postgres, migrations, api, hub
justfile             every development task
scripts/             repository checks
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, commit conventions and the DCO sign-off
every commit needs. Much of the code was written with AI coding assistants; every change goes
through maintainer review, the test suites and CI. Participation is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).

Report security problems privately, as described in [SECURITY.md](SECURITY.md), not in an issue.
Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

Licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for third-party attributions
and [TRADEMARKS.md](TRADEMARKS.md) for use of the Hangar name.
