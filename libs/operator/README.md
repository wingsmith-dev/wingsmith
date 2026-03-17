# operator

Go. One instance per cluster. Dials out to the hub; nothing ever connects inward.

**Two loops, and the split matters.**

- **Sync loop** (`internal/sync`) — stream ↔ CRDs. Applies desired state from the plane into custom
  resources, ships observed state back up.
- **Reconcile loop** (`internal/reconcile`) — CRDs → cluster objects.

The CRDs are the durable local copy of desired state. That is what makes the cluster survive a
plane outage: if the operator held desired state only in memory from the stream, a restart during
a partition would mean the cluster forgets what it is supposed to be running — which would forfeit
the entire reason for choosing an outbound-agent architecture.

## Status

Skeleton and plumbing. The domain object model and the real CRD field sets are owned by another
track and are **not** defined here. Everything under `api/v1alpha1` is a single deliberately
generic placeholder CRD in its own `placeholder.hangar.dev` group, so that it cannot collide with
the real one.

## Layout

```
cmd/operator/        entrypoint: config, logging, both loops, graceful shutdown
api/v1alpha1/        placeholder CRD (SyncedResource) — to be replaced wholesale
internal/config/     env-based configuration
internal/sync/       outbound WebSocket client: framing, correlation, resume, backpressure
internal/reconcile/  controller-runtime manager, no-op reconciler, and the stream→CRD store
internal/health/     /healthz and /readyz
deploy/              kustomize bundle: namespace, CRD, RBAC, config, deployment
```

## The wire

Every WebSocket text frame is one JSON `Envelope`:

| Field | Meaning |
|---|---|
| `type` | `hello`, `hello.ack`, `ping`, `pong`, `desired`, `observed`, `ack`, `error` |
| `id` | unique per envelope, stable across replay |
| `correlation_id` | set on a reply to the `id` it answers |
| `seq` | the sender's monotonic sequence, per direction, continuing across reconnects |
| `ack` | highest contiguous peer sequence the sender has durably processed |
| `ts` | creation time, not refreshed on replay |
| `payload` | type-specific body — **schema owned by the contract track** |

`ping`, `pong`, `ack`, `hello` and `hello.ack` are unsequenced: a heartbeat must never widen a
replay window.

**Resume.** Each direction numbers its own stream. The operator persists the highest contiguous
sequence it has durably applied and offers it in `hello` alongside the session id; the hub replays
from there. If the hub cannot honour the resume point it issues a new session id, the operator
resets its cursor and calls `Resync`. Upward, sequenced envelopes are held in a replay buffer until
the hub's `ack` releases them, and are resent after the handshake.

**Backpressure.** One bounded queue sits between every producer and the single writer. The overflow
policy is explicit (`HANGAR_SEND_QUEUE_POLICY`): `drop-oldest` keeps the freshest state,
`drop-newest` preserves the backlog, `disconnect` refuses to lose anything and tears the stream
down so a resume repairs it. Dropped envelopes are still in the replay buffer, so backpressure
costs latency and ordering, not delivery, until the buffer itself overflows.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `HANGAR_CLUSTER_ID` | — | required; plane-assigned cluster identity |
| `HANGAR_HUB_URL` | — | required; `ws://` or `wss://` only |
| `HANGAR_ENROLMENT_TOKEN` | — | static bearer token; **auth is stubbed** |
| `HANGAR_HUB_CA_FILE` | — | PEM bundle for verifying the hub |
| `HANGAR_HUB_INSECURE_SKIP_VERIFY` | `false` | local development only |
| `HANGAR_LOG_LEVEL` / `HANGAR_LOG_FORMAT` | `info` / `json` | structured logging |
| `HANGAR_HEALTH_ADDR` | `:8081` | `/healthz`, `/readyz` |
| `HANGAR_METRICS_ADDR` | `0` | controller-runtime metrics, `0` disables |
| `HANGAR_NAMESPACE` | `hangar-system` | where CRDs and the lease live |
| `HANGAR_SEND_QUEUE_SIZE` | `1024` | outbound queue bound |
| `HANGAR_SEND_QUEUE_POLICY` | `drop-oldest` | `drop-oldest`, `drop-newest`, `disconnect` |
| `HANGAR_HEARTBEAT_INTERVAL` | `10s` | application-level ping interval |
| `HANGAR_LIVENESS_TIMEOUT` | `30s` | silence tolerated before redialling; must exceed the interval |
| `HANGAR_BACKOFF_MIN` / `HANGAR_BACKOFF_MAX` | `500ms` / `30s` | reconnect backoff, full jitter |
| `HANGAR_LEADER_ELECTION` | `false` | controller-runtime leader election |

Liveness is unconditional and readiness carries the hub session: a partitioned operator is healthy,
because the cluster is meant to keep working when the plane is unreachable.

## Development

```
make all                 # generate, manifests, build, test
make test                # go test -race ./...
make kind-load deploy    # build, load into the kind cluster from dev/, apply
```

The sync loop's tests run against an in-process fake hub (`internal/sync/fakehub_test.go`) and
cover reconnect, resume, refused resume, upward replay, liveness timeout, and all three
backpressure policies.
