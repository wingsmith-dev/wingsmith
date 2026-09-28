# ADR 0005 — The hub scales by shedding session state, not by sharing it

**Status:** accepted · 2026-08-19

## Context

The hub keeps a `SessionState` per `session_id` in
[`libs/plane/app/hub/state.py`](../../libs/plane/app/hub/state.py): the inbound cursor, the
outbound counter, and a bounded replay buffer of frames sent but not yet acked. It lives in a
module-level `SessionRegistry` in
[`libs/plane/app/hub/server.py`](../../libs/plane/app/hub/server.py), which is to say it lives in
one process.

That was a deliberate choice and its docstring says so — the operator's cursor must be durable
because it lives beside the CRDs, but the hub's copy is recoverable, since a hub that has lost its
state can answer `full_resync`. What the choice did not record is the operational consequence:
**the hub cannot run more than one replica.** Two pods do not share a registry, so an operator that
reconnects to a different pod than it left is unknown there and resyncs. Nothing in the code, the
manifests, or the docs said this out loud, which made it a constraint waiting to be discovered in
production rather than a decision.

Kubernetes is the deployment environment. A control plane that cannot be scaled past one replica,
and whose single replica loses in-flight session state on every rollout, is not a posture worth
shipping by accident.

Two coherent positions were available:

1. **Share the session state.** Move `SessionState` — cursors and replay buffer — into Postgres, so
   any pod can serve any operator and resume works across replicas.
2. **Shed the session state.** Keep it in process, accept that a cross-pod reconnect costs a
   `full_resync`, and make the loss safe rather than rare.

## Decision

Shed it. `SessionState` stays in memory. Horizontal scaling is achieved by making state loss
correct and cheap, not by making it impossible.

Three things change to make that true:

- **Ownership of a cluster's stream becomes durable, even though the stream's contents do not.** A
  row in Postgres records which session, on which hub instance, currently owns a `cluster_id`,
  claimed by an atomic compare-and-set at `hello` time. A newer claim wins and the displaced
  connection is closed.
- **Abandoned sessions are reaped.** The registry gains a TTL, so a session whose cluster never
  returns stops occupying a replay buffer forever.
- **The scaling model is written down** — in the manifests and in `PROTOCOL.md`, not only here.

## Why

Sharing the state means writing the replay buffer to Postgres: up to 4096 frames per session, each
written on send and deleted on ack, for every connected cluster continuously. That is a high-churn
write load on the one database the entire control plane depends on, and it buys a property the
protocol was explicitly designed not to need. `full_resync` is not a failure path — it is the
documented, correct answer to a lost cursor (PROTOCOL §4), already implemented, already exercised on
every operator restart. Paying a permanent write cost to avoid an occasional resync inverts the
trade.

Shedding also degrades in the right direction. Under option 1 the database is in the path of every
frame, so database trouble becomes stream trouble for every cluster at once. Under option 2 a hub
pod dying costs exactly the clusters attached to it exactly one resync each, and the blast radius of
a rollout is bounded by replica count rather than by fleet size.

The objection to option 2 is that a rollout resyncs every connected cluster, and with a large enough
fleet a simultaneous fleet-wide resync is its own event. This is real, and it is the reason
ownership is durable even though contents are not: without a single-writer guarantee, two pods can
hold live sessions for one cluster and interleave writes to the same rows, which is a correctness
problem rather than a cost problem. Rate-limiting or staggering resync on rollout is a tuning
question left open; sticky routing by `cluster_id` remains available as an optimisation, and the
important part is that it stays an optimisation. Correctness must not depend on it.

## Consequences

The hub may run N replicas. Resume is best-effort and works within a pod; across pods a reconnect
costs a `full_resync`, which is expensive but never wrong.

Single-writer is enforced with a bounded overlap, not instantaneously, and the distinction is worth
stating precisely. Claiming is atomic, so two sessions can never both believe they hold a cluster.
But a displaced session discovers it has lost the claim by polling its own row, so for up to
`HUB_OWNERSHIP_CHECK_SECONDS` it may still be writing. What this closes is the unbounded case — two
pods writing for one cluster indefinitely, which is a correctness problem. What remains is a short
window in which the *older* session, by definition the stale one, can overwrite fresher rows and
flap `reachable` and `last_seen_at`. That window self-heals: the displacing connection resyncs, and
the operator restreams its inventory over anything stale.

Closing the window entirely would mean fencing the writes themselves — carrying the claim identity
into ingestion and rejecting writes from a session that no longer holds it. That is the stronger
design and remains available. It was not done here because the residual damage is transient,
self-correcting, and confined to observed state that the subsequent resync rewrites, which does not
justify putting a claim check in the path of every ingested frame.

This also closes the pre-existing race where two connections for one `cluster_id` could interleave
`reachable` and `last_seen_at` updates within a single process.

`SessionState` remains a cache. Nothing that must survive the loss of a hub pod may be stored only
there.
