# contract

The plane ↔ operator wire contract, and the single source of truth for it.

| File | What |
|---|---|
| [`API.md`](API.md) | The HTTP API `admin` and `hgr` talk to |
| [`PROTOCOL.md`](PROTOCOL.md) | Connection lifecycle, envelope, correlation, resume, liveness, backpressure |
| [`MESSAGES.md`](MESSAGES.md) | Every message type and its payload schema |
| [`fixtures/`](fixtures/) | One canonical JSON document per message type |
| [`devices.json`](devices.json) | The canonical device-class dictionary: one entry per card variant, with the vendor strings that map onto it per dialect |

These messages are **our domain objects** — Cluster, Device, Artifact, Recipe, Deployment and the
rest. Kubernetes types must never appear here. If a message starts to look like a manifest,
something has leaked in the wrong direction and the operator is the thing that should have
absorbed it.

## The fixture rule

We chose WebSocket + JSON over gRPC ([ADR 0001](../../../docs/decisions/0001-websocket-json-over-grpc.md)),
which means the Go and Python type definitions are hand-written and nothing mechanically stops
them drifting apart. `fixtures/` is the replacement: one canonical document per type, and a test
on both sides that round-trips all of them.

**A message type with no fixture is not part of the contract.** Adding a type and adding its
fixture are the same change, not two.

## The ownership rule

No track edits this directory unilaterally. A change here is a change to every other track's
assumptions, so it goes through whoever is holding the contract, and the fixtures move with it.

## The device-class rule

`nvidia.com/gpu.product` says `NVIDIA-H100-80GB-HBM3` and DCGM's `modelName` says
`NVIDIA H100 80GB HBM3` for the same card, and no upstream will ever reconcile the two. So the
naming is ours: [`devices.json`](devices.json) is the only place a vendor string becomes a hangar
device class, and both the operator and the plane read it.

Matching is by normalized form — lowercase, vendor prefix dropped, every run of non-alphanumeric
characters collapsed to one hyphen — so an alias absorbs spelling drift between dialects, and the
same normalization is implemented and tested on both sides. Aliases are listed per dialect anyway,
because a string that is legitimate on the node label is not evidence about what the exporter emits.

An unmapped card is not an error. It falls back to slugifying the vendor string, which is stable
and deterministic, and both sides log the miss once so the table can be grown.
