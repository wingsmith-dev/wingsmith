# 0034: Hosts serve containers through the Docker Engine API

**Status:** accepted · 2026-11-27

## Context

ADR 0030 enrolled bare Linux hosts as supply only. P2.4 lets them serve. The design pass (P2.1)
settled the shape: Docker as the default runtime, plain engine containers on the P8 container
contract (ADR 0031) instead of Dynamo, one binary holding two connections, and a single-tenant MVP.
What was left was how the host talks to Docker, where GPU grants live, how readiness is decided and
how the data path reaches a replica.

## Decision

- **The Engine API over the unix socket, with a small client of our own** (`internal/host/serve`),
  not the Docker SDK: the SDK's dependency tree is larger than the handful of calls used (list,
  create, start, inspect, remove, pull), and the host binary keeps no Kubernetes or Moby imports.
- **One container per replica, `ws-<deployment>-<replica>`**, labelled with the deployment id,
  generation, serving digest, layout and granted GPU UUIDs. A slot whose labels match the
  declaration is left alone; otherwise the old container is removed before the new one is created,
  because it holds the GPUs the new one needs. Prune removes every container of the deployment
  outside the current slots (rename, scale-down).
- **GPU grants are a JSON map under an exclusive flock** in the state directory, keyed by container
  name. Labels carry the same grant, so a lost file is rebuilt from what is running on startup.
  SQLite was not worth a cgo dependency for a map of a few entries.
- **The operator's security rules apply unchanged**: the image allowlist fails closed, images must be
  digest-pinned unless tags are allowed, artifact, project and Hugging Face ids are validated before
  they become paths, and exec probes are refused. Containers run with `no-new-privileges`, all
  capabilities dropped, a read-only root and the model cache mounted read-only.
- **Readiness is the declared httpGet probe, run by wingsmith-host** against the replica's port,
  which Docker publishes on 127.0.0.1 on an ephemeral port. Phases and reasons are the rollout
  vocabulary in MESSAGES.md; no new reason was added. Missing weights report `WeightsNotResident`,
  the existing reason for exactly that, with the path in the message, until prefetch lands (P2.5).
- **The data path is the agent's code, imported.** The forwarding handler moved out of the
  Kubernetes resolver package (`agent/internal/forward`), and `agent/hostlink` runs the tunnel
  supervisor with a loopback resolver mapping deployment id to the ready replicas' ports. The
  operator module requires the agent module by `replace`, so the operator image builds from `libs/`.
- **Placement**: a host refuses Dynamo workloads with `cluster_kind`. Container workloads go through
  the ordinary runtime check, so a host that has not turned serving on (no `runtimes` reported) is
  refused with `cluster_runtime`.

## Consequences

- Serving is opt-in per host (`serve` in host.json or `run --serve`), so hosts enrolled under 0030
  stay supply-only until their operator turns it on.
- A host rollout is replace-in-place with a gap per replica; there is no surge capacity on one box.
- Every project on a host shares one Docker daemon. Multi-tenant isolation is not supported.
