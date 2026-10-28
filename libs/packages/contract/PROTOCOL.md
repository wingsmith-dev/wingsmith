# The plane ↔ operator protocol

One WebSocket, dialled outbound by the operator, carrying desired state down and observed state
up. This document is the contract. Nothing in `libs/*` may change it unilaterally.

Rationale for the transport choice is [ADR 0001](../../../docs/decisions/0001-websocket-json-over-grpc.md).
Because we gave up codegen, four things below are our code instead of a library's: **framing,
correlation, resume, and backpressure**. They are specified here precisely because nothing
enforces them mechanically.

---

## 1. Connection lifecycle

```
operator                                            hub
   │                                                 │
   ├─ WSS connect (Authorization: Bearer <token>) ───►
   │                                                 │
   ├─ hello { cluster_id, operator_version, session_id, resume_from, lossy } ►
   │                                                 │
   ◄─ welcome { session_id, ack_seq, full_resync } ──┤
   │                                                 │
   ├─ ... frames both directions, indefinitely ... ──►
   ◄─────────────────────────────────────────────────┤
   │                                                 │
   ├─ ping (every 10s) ─────────────────────────────►
   ◄─ pong ──────────────────────────────────────────┤
```

`hello` is always the first frame and is never sent again on the same connection. The hub answers
with `welcome` or closes with a typed error. A connection that has not completed the handshake
within 10 seconds is closed.

**Each cluster authenticates with its own enrolment token.** The bearer token on the connect is
the cluster's enrolment token, issued by the plane when the cluster is registered
(`POST /v1/clusters/{ref}/enrolment-token`, [`API.md`](API.md) §6) and stored there only as a hash.
There is no shared or fleet-wide operator credential: anything that is not shaped like an enrolment
token is turned away before the handshake, and a well-formed one that resolves to no cluster gets a
socket that closes with `4401`. The token, not `hello`, decides which cluster the session speaks for:
`hello.cluster_id` must name that same cluster, by uid or by name, or the hub closes with a protocol
error, so one cluster's credential can never write another cluster's observed state.

Whether the cluster may hold a session is asked at `hello` and again on every ownership poll for as
long as the session lives. A cluster that is not registered, has been disabled, or whose enrolment
token was rotated after this session opened is refused with an `error` (`code: unavailable`,
`retryable: true`) and a `4403` close, so disabling a cluster or rotating a leaked token cuts a live
session instead of waiting for the operator to reconnect, and re-enabling it needs no restart on the
cluster's side. The hub's check has no off switch, unlike the API's local `WINGSMITH_ADMIN_AUTH=disabled`: a
port-forward does not protect a socket that cluster agents dial in to over the network.

**`hello.operator_version` is checked against the plane's release line.** A release-shaped version
(`0.4.2`, `v0.4.2`) outside the major the plane speaks is refused with `retryable: false` and a
protocol-error close, because a different major is free to change what a frame means. A version that
is not release-shaped (`dev`, a commit) is a build from source and is admitted, with a log line on
the hub. An operator that omits the field is read as the latter.

## 2. Envelope

Every frame is a single JSON object. There are no bare payloads.

```json
{
  "v": 1,
  "type": "device.observed",
  "id": "01J9X2E5G7QK8V3B0YHN4TZC6M",
  "seq": 4412,
  "ack": 4390,
  "reply_to": null,
  "ts": "2026-08-19T14:02:11.482Z",
  "payload": {}
}
```

| Field | Meaning |
|---|---|
| `v` | envelope version. Bump only for changes to the envelope itself, never for payloads. Each side reads a range of versions (§9). |
| `type` | `<noun>.<verb>`, lowercase, dot-separated. The payload schema is a function of this and nothing else. |
| `id` | ULID, unique per frame per direction. |
| `seq` | monotonic per direction per session, starting at 1, or absent on unsequenced frames. See §4. |
| `ack` | highest **contiguous** `seq` the sender has durably processed from the peer. Piggybacked on any frame. See §4. |
| `reply_to` | the `id` of the frame being answered, or null. See §3. |
| `ts` | RFC 3339 with milliseconds, UTC, sender's clock. Diagnostic only, never used for ordering. |
| `payload` | type-specific object. Never a scalar, never an array, so fields can be added without a version bump. |

Unknown fields are ignored on receipt. Unknown `type` values are **never fatal**: this is what lets
an operator run one version behind the plane. The frame is dropped, and the receiver answers with an
`error` naming the type and `retryable: false`. Dropping in silence would be cheaper but it makes
version skew invisible in exactly the case that matters: a newer sender whose frames are being
discarded has no way to find out, and the first symptom is state that never converges. The `error`
is an ordinary sequenced frame; answering an unknown type therefore consumes a sequence number.

**A dropped frame is still counted.** A receiver that cannot decode a payload (unknown type,
malformed body) must still advance its arrival expectation for that `seq` and acknowledge it.
Otherwise §2 and §4 contradict each other: the next frame arrives as a gap and closes the
connection, which is the failure §2 exists to prevent. Only the envelope is fatal when
undecodable; the payload is not.

An `error` frame is never fatal to an established session, whatever its `retryable` value. Fatality
is reserved for the handshake, where a rejected credential leaves nothing to continue.

## 3. Correlation

Three interaction shapes, all on the one connection:

- **Event**: fire and forget. `reply_to` is null and no response is expected. Most observed
  state is this.
- **Request/response**: the responder sets `reply_to` to the request's `id`. Requests carry a
  deadline in their payload; a responder that cannot meet it must answer with `error`, not
  go quiet.
- **Stream**: a sequence of frames sharing one `reply_to`, terminated by a frame of type
  `stream.end`. Used for full resync and log-shaped data.

There is exactly one generic failure frame:

```json
{ "type": "error", "reply_to": "<id>", "payload": {
    "code": "not_found | invalid | conflict | unavailable | internal | deadline",
    "message": "human readable, never parsed",
    "retryable": true }}
```

## 4. Sequence numbers and resume

Each direction numbers its own stream from 1 and continues across reconnects within a resumed
session. Both sides hold a replay buffer of sent frames.

**Not every frame is sequenced.** `hello`, `welcome`, `ping`, `pong`, and `ack` carry no `seq` and
never enter a replay buffer. Everything else is sequenced, **including `stream.end`**: a prune
decision depends on that frame, so it must not be able to arrive out of order or go missing
quietly.

`ack` exists for the case where a side must release its peer's replay buffer but has nothing else
to send. Under one-way load the heartbeat would otherwise be the only carrier, and holding a
buffer for up to a full ping interval is a needless amount of memory to pin. A heartbeat that consumed sequence numbers would widen the replay
window in proportion to idle time, which is the opposite of what a heartbeat is for.

**Acknowledgement is continuous, not per-session.** Every frame may carry `ack`: the highest
*contiguous* seq the sender has durably processed. Receiving it releases everything at or below it
from the peer's replay buffer. Contiguous is the operative word: a hole is never acknowledged past,
so a frame whose handler failed is simply not acked and will be replayed. **Withholding the ack is
how a receiver signals "I could not apply this"**: there is no negative acknowledgement.

Reading does not stop at a hole. Frames after a failed one are still delivered and applied (they
are idempotent and delivery is at-least-once), but the contiguous cursor stays behind the hole, so
nothing past it is acknowledged. This makes the receiver's arrival expectation and its applied
cursor two different numbers, and conflating them is a live bug: a refused frame would turn the
peer's next ordinary frame into a gap, so withholding an ack would tear the connection down instead
of causing a replay.

It follows that **on resume a receiver must rewind its arrival expectation to `ack_cursor + 1`**.
The peer is about to replay from the last acknowledged point, and a receiver still expecting the
higher number it had already seen would read that replay as a duplicate, drop it, and never advance
its cursor again. The session survives and makes no progress, which is worse than a clean failure.

On reconnect the operator sends `hello` with `resume_from` set to its highest contiguous processed
seq. The hub answers `welcome` with its own `ack_seq`, and either resumes the session or returns a
new `session_id` with `full_resync: true` when the gap exceeds its buffer.

Three conditions force `full_resync`, and `resume_from` is not consulted for any of them: the
`session_id` is one the hub has no record of (a hub restart looks exactly like this), `lossy` is
set, or the requested point has already left the replay buffer. A sequence number from a session the
receiver cannot identify is not a resume request the receiver can evaluate, so it is not treated as
one. Unreleased frames in the
sender's replay buffer are re-sent immediately after `welcome`.

`ack_seq` in `welcome` refers to the session named in that same `welcome`. When the hub grants a
fresh session (`full_resync: true`, or a `session_id` different from the one asked for) both
directions restart numbering at 1 and both replay buffers are discarded. Frames held under the old
numbering are not re-sent; the resync supersedes them, and re-sending them would mean two
descriptions of the same state arriving under two numbering schemes.

`full_resync` means: the hub streams its complete desired state for that cluster, the operator
streams its complete observed state back, and both sides then discard anything absent from that
snapshot. Each side opens its own stream explicitly with `resync.begin` and closes it with
`stream.end`: the `full_resync` flag announces that this is about to happen but does not stand in
for the framing. Without the explicit open, a receiver could not tell a resync snapshot from
ordinary traffic, and the prune at the end is only safe against a stream it knows the boundaries
of. Resync is correct but expensive, and a control plane that resyncs on every blip is how a
control plane takes itself down, so the buffer is sized to cover a rolling restart, not a network
hiccup. `lossy: true` in `hello` means "I cannot serve as a resume base", and the sender must be given a
fresh session instead of being allowed to pretend it can resume. Replay-buffer overflow is one cause;
a durable cursor written mid-resync, describing a local view that was never a coherent point in the
stream, is another. The flag names the consequence instead of any particular cause, because every
cause has the same remedy. `session_id` in `hello` names which
session is being resumed: `resume_from` alone cannot, since a sequence number means nothing
without the session it counts within.

Sequenced frames are processed strictly in order. A duplicate (`seq` below the expected) is
dropped. A gap (`seq` above the expected) closes the connection instead of being accepted as a
hole.

**The resume cursor must be durable.** It lives beside the CRDs, not in memory. An operator that
restarts during a partition and comes back with an in-memory cursor rewinds its own stream, which
forfeits the property the whole architecture exists to provide.

**Resume is per hub instance, not per fleet.** The hub runs N replicas and the *session*
(cursors and replay buffer) lives in one replica's memory, never in Postgres
([ADR 0005](../../../docs/decisions/0005-the-hub-scales-by-shedding-session-state.md)). Reconnecting to
the same pod resumes exactly as described above. Reconnecting to a different pod (which a
Kubernetes Service makes ordinary, not exceptional) finds a pod with no record of the
session and therefore `full_resync: true` under the third condition above; this is expensive but
never wrong, and it is the price of not paying a permanent write cost to avoid it. What *is*
durable across every pod is ownership: one hub session is the writer for a given `cluster_id`,
established by an atomic claim in Postgres at `hello`, so two sessions can never both believe they
hold the same cluster. A hub instance that loses its claim to a newer `hello` elsewhere discovers
this on its own (nothing pushes it a notice, since the session that displaced it may have no
channel back to the one it displaced) and closes the connection, which the operator experiences as
an ordinary drop-and-reconnect.

Because that discovery is by polling, displacement is bounded, not instantaneous: for up to
one poll interval the displaced session may still write. It is by construction the older and
therefore staler of the two, so what that window can produce is a stale row and a flap of
`reachable`, never a divergence that survives, since the displacing connection resyncs and the
operator restreams its inventory over anything stale. Ingestion writes are not themselves fenced
against the claim.

## 5. Liveness

Application-level, not WebSocket ping frames: those are handled by intermediaries and prove
nothing about the peer's event loop.

The operator sends `ping` every **10s**. A peer that has seen no frame of any kind for **30s**
considers the connection dead and closes it. Reconnect backoff is exponential from 1s to 30s with
full jitter. There is no maximum retry count; an operator that cannot reach the hub keeps
reconciling from its CRDs indefinitely, which is the entire point of the architecture.

## 6. Backpressure

Both sides hold a **bounded** send queue. The policy differs by direction and the asymmetry is
deliberate:

- **Operator → hub.** Observed state is a snapshot of the present, so when the queue is full the
  oldest frame of the same `type` and subject is **replaced**, not appended. Falling behind
  degrades resolution, never correctness. Events (`*.event`) are exempt: they are dropped from
  the tail and the drop is counted and reported, because a silently lost event is worse than a
  known-lost one.
- **Hub → operator.** Desired state must not be dropped. When the queue is full the hub closes
  the connection and lets resume sort it out. Correctness over continuity.

When the queue is full and **no frame matches** the incoming one's type and subject, the oldest
frame overall is evicted and counted as a drop. This is recoverable for the same reason the
replacement case is: the sender will observe the state again.

**Sequence numbers are assigned at write time, not when a frame is queued.** A queue that renumbers
nothing can replace and evict freely; a queue holding pre-numbered frames cannot, because every
replacement leaves a hole that the receiver is required by §4 to read as a gap and close the
connection over. The backpressure policy and the gap rule are only compatible under this ordering.

## 6b. Poison frames

Withholding the ack asks for a replay, so it is only the right answer when a replay could succeed:
a database that was down, a lock that was held, a dependency not yet written. A frame that can
**never** be applied is different: a malformed id, a payload that violates a stated invariant. There
is no negative acknowledgement to express that with, so withholding the ack on one would replay it
forever and the stream behind it would never advance.

A permanently unappliable frame is therefore **accepted, discarded, and logged loudly**, exactly as
an unknown type is. The receiver counts the seq, acks it, and records the rejection with enough
detail to identify the sender. This is the one place the protocol trades a lost record for
liveness, and it is deliberate: one dropped observation is a stale row until the next resync, while
a wedged stream is a cluster that has stopped being managed.

Transient failure withholds. Permanent failure accepts and drops. A receiver that cannot tell the
two apart should withhold, because a replay loop is at least visible.

## 7. What must never cross this link

Inference traffic. Logs. Metrics. Model weights. If any of them appear in a payload, the control
plane's availability has become the inference SLA, and the architecture has been lost. Telemetry
goes from cluster to observability stack directly; this link carries **state**, not measurement.

## 8. Message types

Payload schemas live in [`MESSAGES.md`](MESSAGES.md), one canonical example each in
[`fixtures/`](fixtures/). **A type with no fixture is not part of the contract**, and both sides
run a test that round-trips every fixture. That corpus is the only thing standing in for codegen.

## 9. Changing this document

Additive payload fields are safe and need no version bump. Removing or retyping a field, or
changing envelope semantics, requires a new `v` and a period where both versions are spoken. Since
the hub is upgraded before the operators are, the hub is always the side that carries compatibility
code.

**Each side writes one envelope version and reads a range.** The plane and the operator each hold
the version they write and the oldest they still read (`ENVELOPE_VERSION` and
`MIN_ENVELOPE_VERSION` in `libs/plane/app/hub/messages.py`, `EnvelopeVersion` and
`MinEnvelopeVersion` in `libs/operator/internal/wire/envelope.go`), and accept any frame whose `v`
falls in `[min, current]`. Today both are `1`. A version change is rolled out by widening the hub's
range first, then moving the operators, then the hub's writer, then raising the floor once no
operator on the old version is left, never by a flag day. A frame outside the range closes the
connection: the hub answers it with an `error` carrying `retryable: false`, because reconnecting
cannot change the peer's version, and the operator drops the session with an error naming the range
it reads.

There is no version negotiation on `hello`: neither side proposes a version and neither side
answers with one. The range is fixed per build, and the release-line check in §1 is what refuses a
peer too far away for the range to cover.
