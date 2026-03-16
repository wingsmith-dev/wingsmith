# The plane ↔ operator protocol

One WebSocket, dialled outbound by the operator, carrying desired state down and observed state
up. This document is the contract. Nothing in `libs/*` may change it unilaterally.

Rationale for the transport choice is [ADR 0001](../docs/decisions/0001-websocket-json-over-grpc.md).
Because we gave up codegen, four things below are our code rather than a library's: **framing,
correlation, resume, and backpressure**. They are specified here precisely because nothing
enforces them mechanically.

---

## 1. Connection lifecycle

```
operator                                            hub
   │                                                 │
   ├─ WSS connect (Authorization: Bearer <token>) ───►
   │                                                 │
   ├─ hello { cluster_id, agent_version, resume_from } ►
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

**Auth is stubbed.** A static bearer token identifies the cluster; there is no user auth on this
link and no per-cluster credential rotation yet. The seam exists — the hub resolves a token to a
`cluster_id` — so replacing it later touches one function.

## 2. Envelope

Every frame is a single JSON object. There are no bare payloads.

```json
{
  "v": 1,
  "type": "device.observed",
  "id": "01J9X2E5G7QK8V3B0YHN4TZC6M",
  "seq": 4412,
  "reply_to": null,
  "ts": "2026-08-19T14:02:11.482Z",
  "payload": {}
}
```

| Field | Meaning |
|---|---|
| `v` | envelope version. Bump only for changes to the envelope itself, never for payloads. |
| `type` | `<noun>.<verb>`, lowercase, dot-separated. The payload schema is a function of this and nothing else. |
| `id` | ULID, unique per frame per direction. |
| `seq` | monotonic per direction per session, starting at 1. See §4. |
| `reply_to` | the `id` of the frame being answered, or null. See §3. |
| `ts` | RFC 3339 with milliseconds, UTC, sender's clock. Diagnostic only — never used for ordering. |
| `payload` | type-specific object. Never a scalar, never an array, so fields can be added without a version bump. |

Unknown fields are ignored on receipt. Unknown `type` values are logged and dropped, never fatal —
this is what lets an operator run one version behind the plane.

## 3. Correlation

Three interaction shapes, all on the one connection:

- **Event** — fire and forget. `reply_to` is null and no response is expected. Most observed
  state is this.
- **Request/response** — the responder sets `reply_to` to the request's `id`. Requests carry a
  deadline in their payload; a responder that cannot meet it must answer with `error` rather than
  go quiet.
- **Stream** — a sequence of frames sharing one `reply_to`, terminated by a frame of type
  `stream.end`. Used for full resync and log-shaped data.

There is exactly one generic failure frame:

```json
{ "type": "error", "reply_to": "<id>", "payload": {
    "code": "not_found | invalid | conflict | unavailable | internal | deadline",
    "message": "human readable, never parsed",
    "retryable": true }}
```

## 4. Sequence numbers and resume

Each direction maintains its own counter, starting at 1 for a new session and continuing across
reconnects within a resumed session. Both sides keep a bounded replay buffer of sent frames.

On reconnect the operator sends `hello` with `resume_from` set to the highest `seq` it has
**processed** from the hub. The hub answers `welcome` with `ack_seq` — the highest seq it has
processed from the operator — and either replays from `resume_from + 1`, or sets
`full_resync: true` when the gap exceeds its buffer.

`full_resync` means: the hub sends its complete desired state for that cluster as a stream, and
the operator sends its complete observed state back. Both sides then discard anything not present
in that snapshot. Resync is correct but expensive, and a control plane that resyncs everything on
every blip is how a control plane takes itself down — so the buffer is sized to cover a rolling
restart, not a network hiccup.

Frames are processed strictly in `seq` order. A gap is not tolerated: the receiver closes the
connection and reconnects with resume, rather than skipping.

## 5. Liveness

Application-level, not WebSocket ping frames — those are handled by intermediaries and prove
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
  degrades resolution, never correctness. Events (`*.event`) are exempt — they are dropped from
  the tail and the drop is counted and reported, because a silently lost event is worse than a
  known-lost one.
- **Hub → operator.** Desired state must not be dropped. When the queue is full the hub closes
  the connection and lets resume sort it out. Correctness over continuity.

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
changing envelope semantics, requires a new `v` and a period where the hub speaks both. Since the
hub is upgraded before the operators are, the hub is always the side that carries compatibility
code.
