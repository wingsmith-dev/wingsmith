# contract

The plane's two wire contracts (the HTTP API and the plane ↔ operator protocol), and the single
source of truth for both.

| File | What |
|---|---|
| [`API.md`](API.md) | The HTTP API's conventions: response shapes, the principal kinds and what each may call, and what each group of routes means |
| [`REFERENCE.md`](REFERENCE.md) | Every route the plane serves, generated from the plane |
| [`openapi.json`](openapi.json) | The plane's OpenAPI schema, generated from the plane: parameters, bodies and response shapes |
| [`PROTOCOL.md`](PROTOCOL.md) | Connection lifecycle, auth, envelope, correlation, resume, liveness, backpressure, versioning |
| [`MESSAGES.md`](MESSAGES.md) | Every message type and its payload schema |
| [`fixtures/`](fixtures/) | One canonical JSON document per message type |
| [`devices.json`](devices.json) | The canonical device-class dictionary: one entry per card variant, with the vendor strings that map onto it per dialect |

`openapi.json` and `REFERENCE.md` are generated, never edited by hand. After changing a route on the
plane, run `scripts/openapi.sh` to regenerate both; `scripts/openapi.sh --check` writes nothing and
fails when either is stale or when `API.md`'s resources table names a route group the plane does not
serve, or misses one it does. It runs in the plane's environment through `uv` and needs no database.
The plane's test suite runs the same check (`libs/plane/tests/test_contract_openapi.py`), so a route
change that leaves either file stale fails CI.

## The protocol contract

These messages are **our domain objects**: Cluster, Device, Artifact, Workload, Deployment and the
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

A change here changes what every component on the other side of the wire assumes, so it is made as
a contract change of its own (the plane, the operator and the fixtures move together in it) and
never as a side effect of work on one component.

## The device-class rule

`nvidia.com/gpu.product` says `NVIDIA-H100-80GB-HBM3` and DCGM's `modelName` says
`NVIDIA H100 80GB HBM3` for the same card, and no upstream will ever reconcile the two. So the
naming is ours: [`devices.json`](devices.json) is the only place a vendor string becomes a hangar
device class, and both the operator and the plane read it.

Matching is by normalized form (lowercase, vendor prefix dropped, every run of non-alphanumeric
characters collapsed to one hyphen), so an alias absorbs spelling drift between dialects, and the
same normalization is implemented and tested on both sides. Aliases are listed per dialect anyway,
because a string that is legitimate on the node label is not evidence about what the exporter emits.

An unmapped card is not an error. It falls back to slugifying the vendor string, which is stable
and deterministic, and both sides log the miss once so the table can be grown.

## Roadmap: shared and generated types for the HTTP API

The Go clients of the HTTP API each declare the wire types they read by hand: `hgr`
(`libs/cli/internal/api`), the gateway (`libs/gateway/internal/plane`) and the operator
(`libs/operator/internal/sync`). `libs/transit`'s fake plane mirrors the gateway's. Nothing but
review keeps those in step today, except for the fake plane, whose test reads the gateway's client
source and fails when a JSON field or route exists on one side and not the other.

The intended end state is a Go module here holding the types for the routes more than one module
reads (the resolved routing table, the gateway tenant table and tunnel verification), with `hgr`'s
types generated from `openapi.json`. The console's wire types
(`libs/console/src/modules/*/types.ts`) are hand-written too and are meant to be generated from the
same schema. Neither is built yet.
