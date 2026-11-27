# 0027: The operator drives workloads through a backend seam

**Status:** accepted · 2026-11-27

## Context

The operator's protocol side (hub session, wire messages, resume cursor, reporting) knows nothing
about Kubernetes, but the code that applies a deployment and reads it back was written directly
against controller-runtime and Dynamo. A Linux host agent (P2) needs the same protocol side with a
different runtime underneath: plain engine containers per P8's `runtime.kind: container`, no
Dynamo, no API server.

## Decision

- **`internal/backend.Backend` is the seam:** `Apply(Desired) -> Applied`, `Observe(deployment) ->
  Rollout`, `Inventory() -> Inventory`. `Rollout` uses the existing phase / closed-set reason /
  message vocabulary of MESSAGES.md; `Inventory` is the projected nodes, devices, topology and
  capacity. A refusal that will recur unchanged is a `*backend.Rejected{Reason, Message}`, which
  the caller reports as failed. The package imports no Kubernetes code.
- **Kubernetes/Dynamo is the only implementation today** (`reconcile.KubeBackend`, with
  `observe.Observer.Inventory` as its supply half). The reconciler calls `Apply` and `Observe`
  through it; the ordering gates (artifact declared, weights resident, capacity, multinode) and
  the SyncedResource status handling stay in the reconciler for now.
- **The host backend (P2.3 inventory, P2.4 serving) implements the same interface** with plain
  engine containers. Disaggregated topology is rejected there with a condition.
- **The resume cursor has a file store.** `checkpoint.FileStore` enforces the same no-gap,
  no-rewind rules as `CRDStore` and writes via temp file, fsync, rename, directory fsync, so a
  crash leaves the old or the new cursor. A file that does not decode is an error, never a first
  boot. Both plug into the sync client through `checkpoint.Adapt`.

## Consequences

- `wire`, `sync`, `devices` and `backend` have no Kubernetes imports. `checkpoint` does, through
  `crd.go`, and `observe` does, through its Kubernetes discoverer and taint conversion; the host
  agent will need those split out (CRD store and kube discoverer into their own packages)
  before it can link without client-go.
- Moving the gates behind the seam is left to P2.4, when there is a second implementation to
  shape them against.
