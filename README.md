# Hangar

A control plane for running inference across many GPU clusters. Hangar handles cluster enrolment,
capacity, placement, model lifecycle, serving configuration, quota and operations for a fleet that
spans clusters and regions. It is built for model serving first, and its object model also carries
batch and training workloads.

It is for platform, SRE and ML infrastructure teams that operate GPU clusters their organisation
owns and serve models from them to internal teams or customers.

> **Status: pre-release.** Nothing is published yet: no tagged release, no images, no chart. APIs,
> CRDs and the wire protocol will change without notice.

## How it works

The control plane stores **intent** and each cluster stores **truth**. An operator runs in every
cluster, dials out to the control plane over a single WebSocket, receives Hangar's own domain
objects (never Kubernetes manifests), renders them into whatever the cluster runs, and reports what
it observes back up. Nothing ever dials into a cluster, and a cluster keeps serving when the control
plane is down. Inference traffic never passes through the control plane: a gateway takes
OpenAI-compatible requests and forwards them over an HTTP/2 tunnel that an agent in each cluster
dials out to, using a routing table the plane derives and the gateway caches. Cluster selection is
global and lives in the plane; device selection is local and stays in the cluster.

```
console ──http──► api ◄──► Postgres ◄──► hub ◄── operator (one per cluster, dials out)
hgr     ──http──►

caller ──► gateway ◄── tunnel ── agent (one per cluster, dials out) ──► model frontend
```

The full design, with the reasoning behind each choice, is in
[`docs/architecture/`](docs/architecture/README.md). Individual decisions are recorded in
[`docs/decisions/`](docs/decisions/).

## Components

| Path | Language | What it is |
|---|---|---|
| [`libs/plane`](libs/plane/README.md) | Python | The control plane: the HTTP API, the **hub** that terminates operator streams, and the **reactor** that derives alerts. One package, three processes, one Postgres. |
| [`libs/operator`](libs/operator/README.md) | Go | `hangar-operator`, one per cluster. Syncs desired state from the hub into CRDs, reconciles CRDs into cluster objects, and reports observed state. |
| [`libs/gateway`](libs/gateway/README.md) | Go | The data plane. One OpenAI-compatible hop that routes on `model`, terminates the agents' tunnels, and identifies tenants. Keeps serving from its cached routing table if the plane is down. |
| [`libs/agent`](libs/agent/README.md) | Go | `hangar-agent`, one per cluster. Dials out to every gateway replica and proxies inference to the model's frontend inside the cluster. |
| [`libs/console`](libs/console/README.md) | TypeScript | The web console (React, TanStack Router). Talks to the plane's HTTP API only. Runs against built-in fixtures with no backend. |
| [`libs/cli`](libs/cli/README.md) | Go | `hgr`, the command line. Same API as the console, with stable JSON output and an offline `--fake` backend. |
| [`libs/chart`](libs/chart/cluster-setup/README.md) | Helm | `cluster-setup`, everything a cluster installs to be managed by Hangar: the operator, the agent, CRDs, the DCGM exporter, and optional monitoring. |
| [`libs/packages/contract`](libs/packages/contract/README.md) | Markdown, JSON | The plane-operator wire protocol, the HTTP API's conventions with its generated [route reference](libs/packages/contract/REFERENCE.md) and [OpenAPI schema](libs/packages/contract/openapi.json), the device-class table, and the fixture corpus both sides test against. |
| [`libs/packages/tunnel`](libs/packages/tunnel) | Go | Both ends of the gateway-agent tunnel. Its own module because neither side owns it. |
| [`hangar-runtime`](hangar-runtime/README.md) | Python, Docker | Patches and image layers carried on top of vendor inference images, each with the condition under which it is deleted. |
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
just seed       # create the tenants a fresh database needs (there is no API to create one)
just fleet      # three fake clusters that enrol themselves when registered in the console
```

| Process | Address |
|---|---|
| API | `http://localhost:8000` |
| Hub | `ws://localhost:8001/v1/hub/sync` |
| Console | `http://localhost:5273` |
| Postgres | `localhost:5442` (`hangar`/`hangar`) |

Compose runs with `HANGAR_APP_ENV=local`, which makes the operator token and the admin password
optional, so a fresh checkout runs with no configuration. `just dev-up` starts the same stack without
the console, `just logs` follows it, `just dev-down` stops it, and `just reseed` drops the database
and starts over.

For a real operator against a local Kubernetes cluster, `just cluster-up` creates a kind cluster from
`dev/kind-cluster.yaml`, and `libs/operator/Makefile` builds and loads the operator into it.

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

## Repository layout

```
libs/                one directory per component, each with its own README
docs/architecture/   how the system is put together, and why
docs/decisions/      architecture decision records
dev/                 local-development scripts: seed data, the fake fleet, the kind config
compose.yml          the local control plane: Postgres, migrations, api, hub
justfile             every development task
scripts/             repository checks
```

## Contributing

Enable the repository hooks once per clone:

```sh
git config core.hooksPath .githooks
```

Run `just check` before sending a change.

## License

Licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for third-party attributions.
