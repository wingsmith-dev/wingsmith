# cluster-setup

The tier-2 install that turns a cluster the cloud already prepared (GPUs
schedulable, storage classes bound, a TLS edge) into one the hangar plane can
manage. It installs, in one release:

- the workload namespace (`dynamo` by default) and the shared `model-cache` PVC the operator mounts by name, both kept on `helm uninstall`;
- **dcgm-exporter** (subchart) — the exporter the operator scrapes for device identity;
- the **Dynamo platform** (subchart) — operator, NATS, and the `DynamoGraphDeployment` CRD;
- the **hangar operator** and its CRDs, RBAC, config, and Deployment;
- optionally, the **serving agent** — the cluster's end of the inference tunnel;
- optionally, the serving `Certificate` + Traefik `IngressRoute`.

## Install

```sh
# one-time: authenticate Helm to the chart registry
helm registry login ghcr.io

helm upgrade --install hangar-cluster \
  oci://ghcr.io/hangar-dev/charts/cluster-setup --version <v> \
  --namespace hangar-system --create-namespace \
  -f values.yaml
```

`values.yaml` is one file the register-cluster screen renders and you edit. The
top block is filled in — cluster id, hub URL, operator image, and (when you
generate the enrolment token there) `enrolmentToken.value`, so the chart creates
the operator secret itself. Below a divider is a commented skeleton of this
cluster's own infra facts left blank for you to fill: the pool label scheme, the
RWX storage class, the Hugging Face token (`hfToken.create: true` +
`hfToken.value`, which makes the chart create `hf-token-secret`), and the serving
host. Generate the token, download the values, edit the blanks, install. Only
when driving the chart by hand (no plane) do you set
`enrolmentToken.existingSecret` and create that secret yourself.

## Frontend topology

`frontend.mode` is the one switch, and it takes three values:

| mode | what a request lands on |
|---|---|
| `per-model` (default) | the operator renders a Frontend into each model's own graph, on that model's own image — ADR 0013 |
| `shared` | the one cluster-wide frontend this chart installs; every model must be servable by its single image |
| `none` | neither; for a first install, or beside `agent.frontendEndpoint` for a frontend installed elsewhere |

Mode-specific knobs live under the mode: `frontend.perModel.*` (read by the
operator) and `frontend.shared.*` (rendered by this chart). `replicas`,
`imagePullSecrets` and `anthropicApi` are common to both and stay at
`frontend.*`.

**The first install of a new cluster must use `frontend.mode=none`.** A frontend
is a `DynamoGraphDeployment` in either topology, and that CRD is applied by the
Dynamo operator at runtime rather than shipped in this chart's `crds/`, so a
fresh install that renders one fails with `no matches for kind
DynamoGraphDeployment`. Install once with `--set frontend.mode=none --set
agent.enabled=false`, then upgrade naming the topology and turning the agent on.

**Changing the mode later is not a configuration change.** Dynamo holds a graph's
component set immutable after creation, so every existing graph must be deleted
and re-rendered, and every model reloads its weights. Pick before the first model
lands.

`frontend.enabled` and `operator.frontend.enabled` are gone: they were two
booleans, in two blocks, for one choice, and could contradict each other. The
chart fails with migration instructions if either is set.

To pin the platform singletons off the GPU pool, the 1.4.x subcharts take:

```yaml
dcgm-exporter:
  nodeSelector: { <pool-label>: gpu }
dynamo-platform:
  dynamo-operator:
    controllerManager:
      affinity: { ... }        # the operator subchart takes affinity/tolerations, not nodeSelector
  nats:
    podTemplate:
      merge: { spec: { nodeSelector: { <pool-label>: system } } }
```

(A tainted GPU pool already excludes untolerated singletons, so this is
belt-and-suspenders.)

`cluster.id` and `cluster.hubUrl` are required; the chart refuses to render
without them. See `values.yaml` for the full surface.

## How inference gets in

Nothing dials into a GPU cluster. The **serving agent** is a second Deployment — its own
binary, its own pod, never a container beside the operator — that dials out to the plane's gateway
and then serves HTTP/2 on those connections while the gateway drives the client end.
Requests arrive down a connection this cluster opened, and are reverse proxied to the local
inference frontend.

```yaml
cluster:
  readOnly: false          # required: a read-only cluster reconciles nothing, so it cannot serve
agent:
  enabled: true
  replicas: 2
  gateway:
    address: hangar.example.com:443   # one address, however many replicas the plane runs
  frontendEndpoint: ""     # empty discovers the Dynamo frontend Service by label
```

Four things the chart refuses rather than accepts:

- `agent.enabled` with `cluster.readOnly: true`. Serving what the plane declared means the
  operator applying it, and read-only applies nothing — so the agent would hold healthy
  tunnels, advertise this cluster as reachable, and 502 every request against a frontend that was
  never deployed.
- `agent.enabled` with an empty `gateway.address`. An agent with nowhere to dial installs
  cleanly and carries nothing. An address holding a comma-separated list is refused too — earlier
  values named every replica, and that is no longer how they are found.
- an `agent.gateway` that is still a flat string, or an address carrying a scheme. It is
  `host:port`: TLS is assumed and not optional, so `https://` conveys nothing and resolves as part
  of the hostname.
- an empty `enrolmentToken.value` (already true for the operator). The agent presents the same
  token as its identity on the tunnel — TLS for confidentiality, the token for who is calling —
  so there is no second credential to mint or rotate.

One address, and it stays one. Coverage does have to be complete — the tunnel pool is in-process,
so a gateway replica can only answer a request for this cluster if it is itself holding one of this
cluster's tunnels — but the replicas are not named here. Each accepted handshake says which replica
took the dial and how many there are, so the agent redials this one address until it has found them
all. Scaling the gateway is a change in the plane's own repository and never an edit to this file,
which is the point: a fleet where some clusters got the edit and others did not would be served by
different numbers of replicas with nothing anywhere saying so.

`serving.enabled` — the Traefik `IngressRoute` and its `Certificate` — is the older public-ingress
path and stays as the fallback. It is off on every cluster we run.

## Operator permissions

The operator's RBAC is cut to what the install lets it do, so `cluster.readOnly`
is a permission boundary and not only a setting the operator honours:

| granted | when | scope |
|---|---|---|
| read nodes, pods, deployments, statefulsets | always | cluster |
| write its own CRDs, lease and events | always | release namespace |
| write DynamoGraphDeployments, prefetch Jobs; delete stale DynamoComponentDeployments | `readOnly: false` | workload namespace |
| create benchmark Jobs, read their logs, list Services | `benchmark` allowed | workload namespace |
| patch nodes | `cordon` or `drain` allowed | cluster |
| evict pods | `drain` allowed | cluster |

On a read-only cluster the operations come from `cluster.allowedOperations`
(empty by default, so none); on a managed cluster all three are on. A read-only
install with no operations has no write access outside the release namespace.

What this does not bound: on a managed cluster the plane decides what runs in
the workload namespace, including the engine and benchmark images, so whoever
holds admin access to the plane can run code there. Treat plane admin as
equivalent to write access to that namespace.

## Operator metrics

`operator.metrics.enabled` (on by default) serves Prometheus metrics on
`:8080/metrics` in the operator pod. Set `operator.metrics.podMonitor.enabled`
to have a Prometheus Operator scrape it; the bundled metrics agent does not.

The operator registers no series of its own yet. What it exposes are
controller-runtime's standard families:

| series | labels | what it says |
|---|---|---|
| `controller_runtime_reconcile_total` | `controller`, `result` | reconciles, by outcome |
| `controller_runtime_reconcile_errors_total` | `controller` | reconciles that returned an error |
| `controller_runtime_terminal_reconcile_errors_total` | `controller` | errors that will not be retried |
| `controller_runtime_reconcile_panics_total` | `controller` | reconciles that panicked |
| `controller_runtime_reconcile_time_seconds` | `controller` | reconcile latency (histogram) |
| `controller_runtime_max_concurrent_reconciles`, `controller_runtime_active_workers` | `controller` | worker capacity and use |
| `workqueue_depth`, `workqueue_adds_total`, `workqueue_retries_total` | `name`, `controller` | queue size and churn |
| `workqueue_queue_duration_seconds`, `workqueue_work_duration_seconds` | `name`, `controller` | time waiting and time working (histograms) |
| `workqueue_unfinished_work_seconds`, `workqueue_longest_running_processor_seconds` | `name`, `controller` | stuck work |
| `rest_client_requests_total` | `code`, `method`, `host` | Kubernetes API calls |
| `leader_election_master_status` | `name` | 1 on the replica holding the lease |
| `go_*`, `process_*` | | Go runtime and process |

A read-only operator runs no reconciler, so the `controller_runtime_*` families
are absent there. Hub-session health is not a metric yet; it is the operator's
readiness (`/readyz`), and the plane reports it per cluster.

## Versioning

The chart follows SemVer, and `appVersion` always equals `version`. The
operator and serving-agent images default to that tag (`operator.image.tag`
and `agent.image.tag` empty), with `pullPolicy: IfNotPresent`, so one chart
version runs one build of each and a node never re-pulls a moving tag. Set a tag
only to run a build of your own; a mismatched operator and chart is not a
supported combination.

What the chart does pin:

| component | version |
|---|---|
| hangar operator, serving agent | `appVersion` |
| `dynamo-platform` subchart (Dynamo operator, NATS) | 1.4.1 |
| shared-mode frontend image (`frontend.shared.image.tag`) | 1.4.1, kept equal to the subchart |
| `dcgm-exporter` subchart | 4.8.3 |
| metrics agent (Prometheus, agent mode) | v3.5.5 |
| Kubernetes | 1.29 or later |

The Dynamo version is the one the operator renders `DynamoGraphDeployment`s
for, and it is what an engine runtime image must be built against: a model's
`engine_runtime_image` (and a per-model frontend image) has to come from the
same Dynamo release as the platform, or the workers and the frontend disagree on
the wire. Changing it is a chart release, not a values edit.

Release notes, including anything breaking or deprecated and any CRD you must
apply by hand, are in [CHANGELOG.md](CHANGELOG.md).

## CRDs

The chart ships two CRDs in `crds/`, both in the `hangar.dev` API group at
`v1alpha1`: `SyncedResource` (the operator's local copy of what the plane
declared) and `StreamCheckpoint` (its resume cursor on the hub stream). They are
the operator's own state, not an API for users to write to, and `v1alpha1`
means what it says: the schema can change between releases without a
conversion webhook. `hangar.dev` is an interim group name and may change before
a stable release.

Helm installs `crds/` on the first `helm install` and never touches it again: a
`helm upgrade` does not update a CRD that changed, does not install a CRD that
is new, and `helm uninstall` does not remove them. When a release notes a CRD
change, apply it yourself before upgrading:

```sh
kubectl apply --server-side -f crds/
```

## Maintaining

The hangar CRDs in `crds/` are a copy of the source of truth in
`libs/operator/deploy/crd/`, so run `just chart-sync-crds` whenever those
change. `just
chart-lint` renders the chart offline; `just chart-push` packages and publishes
it. The subcharts (`dcgm-exporter`, `dynamo-platform`) pull anonymously — no NGC
account is needed. The Dynamo operator applies the `DynamoGraphDeployment` CRD
from its own image (`upgradeCRD`, on by default), so there is no separate CRD
chart to vendor.
