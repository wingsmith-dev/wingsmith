# 0027: The operator drives workloads through a backend seam

**Status:** accepted · 2026-11-27

## Context

The operator's protocol side (hub session, wire messages, resume cursor, reporting) knows nothing
about Kubernetes, but the code that applies a deployment and reads it back was written directly
against controller-runtime and Dynamo. A Linux host agent (P2) needs the same protocol side with a
different runtime underneath: plain engine containers per P8's `runtime.kind: container`, no
Dynamo, no API server.

## Decision

- **`internal/backend.Backend` is the seam:** `Apply(Desired) -> Applied`, `Prune(deployment,
  current)`, `Delete(deploymentID)`, `Observe(deployment) -> Rollout`, `Inventory() -> Inventory`.
  `Prune` runs after every pass that reaches a current workload, written or not, and collects
  what earlier applies left behind (on Kubernetes: graph deployments under an old name, component
  deployments Dynamo abandoned). `Delete` is teardown, by deployment id; absent is success. This
  lines up with P8's `WorkloadRenderer` (Render/Observe/Delete). `Rollout` uses the existing phase / closed-set reason /
  message vocabulary of MESSAGES.md; `Inventory` is the projected nodes, devices, topology and
  capacity. A refusal that will recur unchanged is a `*backend.Rejected{Reason, Message}`, which
  the caller reports as failed. The package imports no Kubernetes code.
- **Kubernetes/Dynamo is the only implementation today** (`reconcile.KubeBackend`, with
  `observe.Observer.Inventory` as its supply half). The reconciler calls `Apply`, `Prune`, `Delete`
  and `Observe` through it, and the observer is wired in as `Supply`; the ordering gates (artifact declared, weights resident, capacity, multinode) and
  the SyncedResource status handling stay in the reconciler for now.
- **The host backend (P2.3 inventory, P2.4 serving) implements the same interface** with plain
  engine containers. Disaggregated topology is rejected there with a condition.
- **The resume cursor has a file store.** `checkpoint.FileStore` enforces the same no-gap,
  no-rewind rules as `CRDStore` and writes via temp file, fsync, rename, directory fsync, so a
  crash leaves the old or the new cursor. A file that does not decode is an error, never a first
  boot. `Load` takes an exclusive, non-blocking flock on a sidecar `<path>.lock`, held until
  `Close`, so a second process on the same path fails with `ErrLocked`. Both plug into the sync client through `checkpoint.Adapt`.

## Consequences

- `wire`, `sync`, `devices` and `backend` have no Kubernetes imports. `checkpoint` does, through
  `crd.go`, and `observe` does, through its Kubernetes discoverer and taint conversion; the host
  agent will need those split out (CRD store and kube discoverer into their own packages)
  before it can link without client-go.
- Moving the gates behind the seam is left to P2.4, when there is a second implementation to
  shape them against.
