# operator

The Hangar cluster operator. Go, one instance per Kubernetes cluster. It dials out to the plane's
hub over one WebSocket and nothing ever connects inward.

It does five things:

- **Sync** (`internal/sync`, `internal/wire`): holds the hub session. Declared state from the plane
  lands in `SyncedResource` custom resources; observed state and operation results go back up.
- **Render and reconcile** (`internal/reconcile`, `internal/render`): turns each declared
  deployment into a Dynamo `DynamoGraphDeployment` in the workload namespace, prefetches model
  weights with a Job before any worker starts, checks there is capacity for a replica, and tears
  down what it rendered when the declaration goes away.
- **Observe** (`internal/observe`): reads nodes, accelerators (from the DCGM exporter), topology,
  capacity and workloads it did not render, and reports them as `*.observed` and `*.event` frames.
- **Operate** (`internal/operate`): runs operations the plane sends: `cordon`, `drain` and
  `benchmark`. An operation runs to a conclusion and is reported; it is never stored as declared
  state.
- **Checkpoint** (`internal/checkpoint`): keeps the stream's resume cursor in a `StreamCheckpoint`
  resource, so a restart during a plane outage resumes where it left off.

The `SyncedResource` objects are the durable local copy of declared state. That is what lets a
cluster keep running what it was told to run while the plane is unreachable, including across an
operator restart.

## Custom resources

Both resources are in the `hangar.dev` API group at `v1alpha1`:

| Kind | Short name | What it is |
|---|---|---|
| `SyncedResource` | `syncres` | One declared object from the plane: a deployment, an artifact, a cache or a cluster component. The payload is kept as it arrived. |
| `StreamCheckpoint` | `streamcp` | The hub stream's resume cursor. Written only by the sync loop. |

The group is one constant, `api.Group` in `api/group.go`. Kubebuilder markers cannot reference a
constant, so the `+groupName` and `+kubebuilder:rbac` markers spell it too; the comment beside the
constant lists them.

`v1alpha1` has no conversion webhook and no stored-version migration. Until there is a stable
version, a change to the group or the schema means reinstalling the CRDs: delete every
`SyncedResource` while the old operator is still running (see Uninstall), delete the old CRDs,
install the new ones and the new operator, and let the hub's full resync recreate the declared
state. Helm does not upgrade anything under a chart's `crds/` directory, so apply the new CRDs with
`kubectl apply -f deploy/crd/` before upgrading the chart.

## How a deployment is reconciled

A declared deployment is rendered only when its `authority` is `managed`; `observed` (the default)
records it and writes nothing to the cluster. An adopted deployment, one promoted from a workload
that was already running, is never rendered whatever its authority: the operator checks that the
live object still matches the fingerprint it was adopted at and reports what it is running.

A new revision passes four gates before it is applied: its artifact has arrived, the weights have
finished downloading on this cluster, a replica fits on the nodes (counting only accelerators that
are free and nodes whose taints the worker tolerates), and a replica that needs more than one node
has a leader-worker backend to run on. Each failing gate leaves the deployment `Waiting` with a
reason. Once the graph deployment is running from the declared generation and serving digest, the
gates are not asked again, so a drain or a transient read failure does not move a running
deployment back to `Waiting`.

Nothing is applied when the live graph was already built from the same generation, serving digest
and layout. A change reaches a running deployment only through a new declaration.

## The wire

`libs/packages/contract/PROTOCOL.md` and `libs/packages/contract/MESSAGES.md` are the authority;
`internal/wire` is the Go half of them, kept in step by the fixture corpus in
`libs/packages/contract/fixtures`. The operator reads every envelope version from
`wire.MinEnvelopeVersion` to `wire.EnvelopeVersion`.

The bearer token travels as an `Authorization` header on the WebSocket handshake. It is the
per-cluster enrolment token the plane mints when the cluster is registered; the hub opens a session
for no other credential.

**Resume.** Each direction numbers its own stream. The operator persists the highest contiguous
sequence it has durably applied and offers it in `hello`. The hub either resumes or starts a new
session with `full_resync`, in which case the operator resets its cursor, opens a resync epoch and
streams its own observed inventory. That resync runs after the handshake, under its own deadline.
A frame the operator fails to apply is retried in place a few times; if it still fails, the session
ends and the resume replays it.

**Backpressure.** One queue sits between every producer and the single writer. Below its bound
(`HANGAR_SEND_QUEUE_SIZE`) every frame is appended. Above it an event is dropped and counted, and
an uncorrelated observation replaces the queued one for the same subject. Nothing queued is
evicted. A hard limit of sixteen times the bound (at least 16384 frames) refuses the frames a later
resync restates (stream openers and terminators, frames inside a resync stream, replies) and ends
the session; the frames already queued go out on the next one. Uncorrelated observations,
operation and benchmark reports among them, are still admitted past the limit, one per subject,
so a terminal report is not lost to backpressure. The queue is in memory, so a restart still loses
whatever it held.

## Replicas and leader election

Run one replica. Leader election is on by default and guards against two operators running at
once during a rollout: the controller, the hub session, the observation loop and the periodic demand
resync all run only in the replica holding the lease, and the lease is released on shutdown. A
replica waiting for the lease reports not ready because it holds no hub session.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `HANGAR_CLUSTER_ID` | required | the plane-assigned cluster id |
| `HANGAR_HUB_URL` | required | `wss://` hub address; `ws://` only with `HANGAR_HUB_ALLOW_INSECURE=true` |
| `HANGAR_HUB_ALLOW_INSECURE` | `false` | accept a `ws://` hub URL, which sends the token in the clear; local development only |
| `HANGAR_ENROLMENT_TOKEN` | required | the cluster's enrolment token |
| `HANGAR_HUB_CA_FILE` | none | PEM bundle for verifying the hub |
| `HANGAR_HUB_INSECURE_SKIP_VERIFY` | `false` | local development only; logs a warning at startup |
| `HANGAR_NAMESPACE` | `hangar-system` | where the custom resources and the lease live |
| `HANGAR_WORKLOAD_NAMESPACE` | `dynamo` | where rendered workloads and prefetch jobs go |
| `HANGAR_READ_ONLY` | `false` | observe and report only: no reconciler runs and operations are refused |
| `HANGAR_ALLOWED_OPERATIONS` | none | operation kinds a read-only operator still runs, comma-separated |
| `HANGAR_LEADER_ELECTION` | `true` | controller-runtime leader election |
| `HANGAR_LOG_LEVEL` / `HANGAR_LOG_FORMAT` | `info` / `json` | structured logging |
| `HANGAR_HEALTH_ADDR` | `:8081` | `/healthz`, `/readyz` |
| `HANGAR_METRICS_ADDR` | `0` | controller-runtime metrics; `0` disables |
| `HANGAR_SEND_QUEUE_SIZE` | `1024` | outbound queue bound |
| `HANGAR_HEARTBEAT_INTERVAL` | `10s` | application-level ping interval |
| `HANGAR_LIVENESS_TIMEOUT` | `30s` | silence tolerated before redialling; must exceed the interval |
| `HANGAR_BACKOFF_MIN` / `HANGAR_BACKOFF_MAX` | `1s` / `30s` | reconnect backoff, full jitter |
| `HANGAR_OBSERVE_INTERVAL` / `HANGAR_OBSERVE_TIMEOUT` | `30s` / `60s` | observation tick and its deadline |
| `HANGAR_OBSERVE_STALE_AFTER` | `5m` | readiness fails when the last observation is older |
| `HANGAR_RESYNC_INTERVAL` | `15m` | how often the whole observed inventory is restated, which is what retires objects the cluster no longer has |
| `HANGAR_DCGM_ENDPOINT` / `HANGAR_DCGM_SELECTOR` / `HANGAR_DCGM_TIMEOUT` | in-cluster exporter / none / `5s` | where device identity is scraped |
| `HANGAR_POOL_LABEL_KEY` / `HANGAR_GPU_POOL_VALUE` | `hangar.dev/pool` / `gpu` | node label workers are placed by |
| `HANGAR_GPU_TOLERATION_KEY` / `HANGAR_GPU_TOLERATION_VALUE` | `nvidia.com/gpu` / none | the taint GPU workers tolerate; no value tolerates any value of the key |
| `HANGAR_CACHE_POOL_VALUE` / `HANGAR_CACHE_TAINT_KEY` / `HANGAR_CACHE_TAINT_VALUE` | `cache` / `hangar.dev/cache` / none | node pool and taint for weight downloads |
| `HANGAR_PREFETCH_CPU` / `HANGAR_PREFETCH_MEMORY` / `HANGAR_PREFETCH_MEMORY_LIMIT` | none | prefetch job resources |
| `HANGAR_MODEL_CACHE_PVC` / `HANGAR_LOCAL_MODEL_CACHE_DIR` | `model-cache` / none | where weights are cached |
| `HANGAR_ENGINE_PULL_SECRETS` | none | image pull secrets for engine workers |
| `HANGAR_ALLOWED_IMAGE_PREFIXES` | none | registries or repositories every engine, frontend and prefetch image must come from; empty allows any |
| `HANGAR_FRONTEND_ENABLED` | `false` | render one frontend per model |
| `HANGAR_FRONTEND_IMAGE` / `HANGAR_FRONTEND_REPLICAS` / `HANGAR_FRONTEND_PULL_SECRETS` | none / `2` / none | the frontend's image, count and pull secrets |
| `HANGAR_FRONTEND_CPU` / `HANGAR_FRONTEND_MEMORY` / `HANGAR_FRONTEND_MEMORY_LIMIT` | none | frontend resources |
| `HANGAR_FRONTEND_ANTHROPIC_API` / `HANGAR_FRONTEND_ANTHROPIC_STRIP_PREAMBLE` | `true` / `true` | serve `/v1/messages`, and drop a fixed client preamble so the prefix cache can hit |
| `HANGAR_FRONTEND_GLOBAL_DYNAMO_NAMESPACE` | `true` | put each graph's frontend and workers in the shared Dynamo namespace; `false` gives each graph its own. Read only with `HANGAR_FRONTEND_ENABLED` |
| `HANGAR_MULTINODE` | `false` | assert a leader-worker backend (Grove or LeaderWorkerSet) when discovery cannot see it |
| `HANGAR_FABRIC` | none | RDMA fabric workers move KV over: empty, `efa` or `rdma` |
| `HANGAR_FABRIC_DEVICES` / `HANGAR_FABRIC_RESOURCE` | `0` / `vpc.amazonaws.com/efa` under `efa` | interfaces per worker pod and their extended resource; `rdma` requires the resource |
| `HANGAR_FABRIC_PRIVILEGED` | `true` under `efa`, else `false` | run fabric workers privileged with host IPC |
| `HANGAR_EFA_DEVICES` | none | legacy alias for `HANGAR_FABRIC=efa` with that many devices; refused alongside the new keys |
| `HANGAR_NIXL_BACKENDS` | none | NIXL plugins the KV connector registers; `LIBFABRIC` on EFA |
| `HANGAR_INFERENCE_ENDPOINT` | discovered | the frontend a benchmark aims at |
| `HANGAR_BENCHMARK_TIMEOUT` / `HANGAR_BENCHMARK_CPU` / `HANGAR_BENCHMARK_MEMORY` | `30m` / `500m` / `1Gi` | benchmark runs |
| `HANGAR_BENCHMARK_IMAGES` | the pinned aiperf and evalscope images | harness images a benchmark may run, matched exactly |
| `HANGAR_BENCHMARK_TOKEN_SECRET` | none | Secret in the workload namespace handed to benchmark jobs as environment, for a gated tokenizer |
| `HANGAR_BENCHMARK_ARTIFACT_BASE` | none | where raw benchmark records would be uploaded; nothing uploads yet |

The renderer targets NVIDIA GPUs only: workers request `nvidia.com/gpu` (`render.GPUResource`) and
device identity comes from DCGM. Other accelerators are not supported.

Liveness is unconditional and readiness carries the hub session and the observation loop: a
partitioned operator is alive, because the cluster is meant to keep working when the plane is
unreachable.

## Install and uninstall

The Helm chart in `libs/chart/cluster-setup` is the supported install. `deploy/` is a kustomize
bundle for development: namespace, CRDs, generated RBAC, config and deployment. It expects a
`hangar-operator` Secret holding the enrolment token (see `deploy/20-config.yaml`).

A declared deployment carries the `hangar.dev/rendered` finalizer until the operator has removed
what it rendered, so uninstall in this order:

1. Delete the `SyncedResource` objects while the operator is still running, and wait for them to
   go: `kubectl -n hangar-system delete syncedresources --all --wait`.
2. Uninstall the operator (`helm uninstall`, or `make undeploy` for the kustomize bundle).
3. Delete the CRDs: `kubectl delete -f deploy/crd/`.

If the operator is already gone, remove the finalizers by hand and then delete the graph
deployments it rendered (they carry the `hangar.dev/deployment-id` label):

```
kubectl -n hangar-system get syncedresources -o name \
  | xargs -r -n1 kubectl -n hangar-system patch --type merge -p '{"metadata":{"finalizers":null}}'
```

A read-only operator has no reconciler to tear anything down. It runs one small controller instead
that removes the finalizer from any declaration being deleted, whether the hub deleted it or
someone ran step 1 with kubectl, and leaves anything rendered by an earlier read-write run in
place. Delete those graph deployments by label as above.

## Layout

```
cmd/operator/          entrypoint: config, logging, the manager and every loop, shutdown
api/                   the API group constant
api/v1alpha1/          SyncedResource
api/checkpoint/        StreamCheckpoint
internal/config/       environment configuration
internal/sync/         hub client: framing, resume, retry, backpressure
internal/wire/         the Go half of the wire contract
internal/reconcile/    controller, desired-state store, capacity, diagnosis, demand resync
internal/render/       DynamoGraphDeployment, prefetch and benchmark rendering
internal/observe/      observation loop and discovery
internal/operate/      cordon, drain and benchmark
internal/checkpoint/   the durable resume cursor
internal/devices/      device class dictionary
internal/workload/     the pod-template fingerprint adoption is checked against
internal/health/       /healthz and /readyz
internal/interop/      Go client against the Python hub (build tag `interop`)
deploy/                kustomize bundle; crd/ and rbac/ are generated
```

## Development

```
make all                 # generate, manifests, build, test
make test                # go test -race ./...
make vet                 # go vet, including the e2e and interop suites
make verify-manifests    # regenerate and fail if deploy/crd, deploy/rbac or deepcopy changed
make kind-load deploy    # build, load into the kind cluster from dev/, apply
just interop             # from the repository root: the Go client against the real Python hub
```

`deploy/crd/` and `deploy/rbac/role.yaml` are generated from the kubebuilder markers by
`make manifests`; edit the markers, not the YAML. The chart keeps its own copies.

The sync loop's tests run against an in-process fake hub (`internal/sync/fakehub_test.go`). The
reconciler's tests use controller-runtime's fake client; `internal/reconcile/e2e_test.go` (build
tag `e2e`) runs against a real API server for what the fake cannot model, such as garbage
collection and finalizers. There is no envtest suite yet, so that behaviour is only checked against
a real cluster. The interop suite (build tag `interop`) starts the plane's hub through
`internal/interop/testdata/hub.py`, which needs `uv` and the plane's environment.
