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

`contract/PROTOCOL.md` and `contract/MESSAGES.md` are the authority; `internal/wire` is the Go half
of them, kept honest by the fixture corpus in `contract/fixtures`. `internal/sync` holds no wire
types of its own — a second envelope definition would be a second contract, and only one of them
would be tested.

Every WebSocket text frame is one JSON envelope carrying `v`, `type`, `id`, optional `seq` and
`ack`, optional `reply_to`, an RFC 3339 UTC millisecond `ts`, and a type-specific `payload` object.
`hello`, `welcome`, `ping`, `pong` and `ack` are unsequenced; everything else is sequenced,
including `stream.end`. The bearer token travels as an `Authorization` header on the WebSocket
handshake, never in a payload.

**Resume.** Each direction numbers its own stream. The operator persists the highest contiguous
sequence it has durably applied — in a `StreamCheckpoint` CRD, not in memory — and offers it in
`hello` as `resume_from` alongside the session id and the `lossy` flag. The hub answers `welcome`
and either resumes or returns a new session with `full_resync`, in which case the operator resets
its cursor and calls `Resync` before the snapshot streams. A duplicate is dropped; a gap closes the
connection. Upward, sequenced envelopes are held in a replay buffer until
the hub's `ack` releases them, and are resent after the handshake.

**Backpressure.** One bounded queue sits between every producer and the single writer. The policy
is PROTOCOL §6 and is not configurable: a full queue replaces the oldest frame with the same type
and subject, because observed state is a snapshot of the present and falling behind should cost
resolution rather than correctness. Events are exempt — they are dropped from the tail and the loss
is counted. Sequence numbers are assigned by the writer, so a replaced frame never leaves a hole.

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
| `HANGAR_HEARTBEAT_INTERVAL` | `10s` | application-level ping interval |
| `HANGAR_LIVENESS_TIMEOUT` | `30s` | silence tolerated before redialling; must exceed the interval |
| `HANGAR_BACKOFF_MIN` / `HANGAR_BACKOFF_MAX` | `1s` / `30s` | reconnect backoff, full jitter |
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
