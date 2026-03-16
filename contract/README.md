# contract

The plane ↔ operator wire contract, and the single source of truth for it.

| File | What |
|---|---|
| [`PROTOCOL.md`](PROTOCOL.md) | Connection lifecycle, envelope, correlation, resume, liveness, backpressure |
| [`MESSAGES.md`](MESSAGES.md) | Every message type and its payload schema |
| [`fixtures/`](fixtures/) | One canonical JSON document per message type |

These messages are **our domain objects** — Cluster, Device, Artifact, Recipe, Deployment and the
rest. Kubernetes types must never appear here. If a message starts to look like a manifest,
something has leaked in the wrong direction and the operator is the thing that should have
absorbed it.

## The fixture rule

We chose WebSocket + JSON over gRPC ([ADR 0001](../docs/decisions/0001-websocket-json-over-grpc.md)),
which means the Go and Python type definitions are hand-written and nothing mechanically stops
them drifting apart. `fixtures/` is the replacement: one canonical document per type, and a test
on both sides that round-trips all of them.

**A message type with no fixture is not part of the contract.** Adding a type and adding its
fixture are the same change, not two.

## The ownership rule

No track edits this directory unilaterally. A change here is a change to every other track's
assumptions, so it goes through whoever is holding the contract, and the fixtures move with it.
