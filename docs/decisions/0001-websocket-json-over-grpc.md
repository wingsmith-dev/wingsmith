# ADR 0001: WebSocket + JSON for the plane ↔ operator link

**Status:** accepted · 2026-08-19

## Context

The operator holds one long-lived connection to the hub. Desired state flows down:
deployments, component versions, cache warm-sets, operations. Observed state flows up
continuously: devices, health, replica status, events, operation results. Both directions,
one connection, always dialled outbound by the operator.

"Bidirectional" does not discriminate between the candidates: WebSocket is bidirectional too.
The real choice was **protobuf with generated types** against **JSON with hand-written types**.

## Decision

WebSocket with JSON payloads. Message types are hand-written on both sides (msgspec Structs in
Python, structs in Go) and kept in step by a shared golden-fixture corpus instead of by codegen.

## Why

gRPC's value here was never throughput; at tens of clusters that is irrelevant. It was four
things: framing, request/response correlation, flow control, and mechanical protection against
the Go and Python type definitions drifting apart.

Its cost was a protoc/buf toolchain in every build, an unreadable wire during the exact phase
where the architecture is being falsified, debugging that means learning gRPC's keepalive and
deadline semantics instead of reading our own code, and generated Python that sits badly next to
msgspec.

The message count is moderate and the codebase is already fluent in msgspec and JSON. Being able
to tail a connection and read what is on it is worth more right now than efficiency we do not
need. So we take the trade knowingly and write the four things ourselves; see
[`libs/packages/contract/PROTOCOL.md`](../../libs/packages/contract/PROTOCOL.md).

## What this obliges us to build

Framing, resume, liveness, and backpressure are now our code, not a library's. Resume is the one
that gets skipped and regretted: without sequence numbers and a replay buffer, every reconnect
becomes a full resync, and full resyncs at scale are how a control plane takes itself down.

## Drift

Codegen was the only thing gRPC gave us that we cannot simply choose not to need. The replacement
is `libs/packages/contract/fixtures/`: one canonical JSON document per message type, with a test
on both sides that round-trips every fixture. Drift then fails in CI, not silently in
production. A message type with no fixture is not part of the contract.

## Escape hatch

If the message set passes roughly fifty types, or throughput ever becomes real, the payload
encoding can be swapped to protobuf over the same WebSocket. The envelope, the connection
lifecycle, and the resume semantics do not change, so the blast radius is the codec and nothing
else.

## Consequences

- `libs/packages/contract/` is the single source of truth and no component may edit it
  unilaterally. This rule matters more without codegen, not less, because nothing mechanically
  enforces it now.
- Every message type ships with a fixture in the same change.
