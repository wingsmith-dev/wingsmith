# 0030: A Linux host is a cluster of kind host

**Status:** accepted · 2026-11-27

## Context

P2 runs Wingsmith on bare Linux GPU machines with no Kubernetes. The plane already models supply
as clusters with nodes, devices and capacity, and placement, quotas and the console all read that
model. A host needs to fit without the plane learning what Kubernetes is (the observed
`distribution` is display-only, MESSAGES.md).

## Decision

- **A host is a `Cluster` with a declared `kind` of `host`** (`supply_cluster.kind`, `kubernetes`
  by default, check-constrained). It is set at registration and cannot be changed: the two kinds
  enrol different agents. The plane branches on `kind`, never on `distribution`.
- **Exactly one node.** Ingestion replaces a host's node when a node of another name is observed
  (a renamed machine), instead of adding a second. Pools of hosts are many single-node clusters
  sharing labels.
- **Placement is unchanged except for one gate:** until hosts can serve (P2.4), a host is refused
  as a candidate with `cluster_kind`, so it shows as capacity and says why nothing lands on it.
- **`wingsmith-host` lives in the operator module** (`cmd/wingsmith-host`, `internal/host`),
  because it is the operator's protocol side (sync client, file resume cursor, observation loop,
  device table) over a different backend and those packages are internal. It must not link
  client-go: the Kubernetes discoverer and CRD cursor store moved to `observe/kube` and
  `checkpoint/crdstore`, and a test runs `go list -deps` on the binary.
- **Devices come from NVML, with `nvidia-smi -q -x` as fallback.** NVML names are DCGM's
  `modelName` (DCGM reads them from NVML), so the `nvml` dialect shares the DCGM aliases.
- **Enrolment needs the cluster name** (`--cluster`) beside the token and hub URL, because the
  token carries no identity and the handshake checks `cluster_id` against it.

## Consequences

- No new wire message types; a host is distinguishable on the wire only by `distribution: host`,
  which stays inert.
- The host reports `read_only: true` until P2.4 adds the serving backend.
