# 0040: Engine presets are templates for the container runtime

**Status:** accepted · 2026-11-27

## Context

ADR 0031 let a workload run any digest-pinned container. Running stock vLLM or SGLang that way
meant every author retyping the image digest, the serve command, the served-name and port flags,
the tensor-parallel flag matching the shape, and the probes. Linux hosts (P2) have no Dynamo, so
this is also the only way a host serves a text model.

## Decision

- **A preset is a field of `kind: container`, not a runtime kind.** `runtime: {kind: container,
  preset: vllm|sglang, version, args}`. A third kind would need its own renderer, host path and
  discovery capability for something that is a container worker; a template keeps one contract and
  one renderer, the way Kubernetes defaulting fills a spec without inventing a new kind.
- **The spec stays as declared; the expansion is derived.** `runtime_spec` stores the preset,
  version and the user's extra args. The plane expands it wherever the worker is used (validation,
  the desired-state wire, reads) and shows it in `status.runtime` and `serving.runtime.resolved`.
  Clusters and hosts only ever see a plain `ContainerSpec`, so no operator or agent change is needed
  to run a preset.
- **`version` is defaulted on write.** An omitted version is stored as the table's default at create
  time, so changing the default later does not move existing workloads.
- **Images come from a digest-pinned table in plane config.** `ENGINE_PRESET_IMAGES` maps
  `preset@version` to `repo@sha256:...`, like `ENGINE_RUNTIME_REPOS` does for Dynamo. An entry
  without a digest refuses the workload. Entries are append-only: `serving_digest` hashes the
  declared spec, so a version's digest must never be edited in place (that would change what runs
  without a rollout).
- **Tensor parallelism is the shape's device count**, since a container replica is one node.
- **Readiness follows the protocol** (`/v1/models` for `openai-chat`, `/health` otherwise), with a
  long startup probe on `/health`; declared probes, ports, env and metrics win; image and command
  are refused.
- **Worker metrics use what each place already has.** Pods carry `prometheus.io` annotations and
  the bundled metrics agent scrapes them; a `PodMonitor` is rendered only behind
  `operator.workerMetrics.podMonitor` and only when the CRD is served. Hosts publish metrics ports
  on loopback and the agent serves them as Prometheus HTTP SD on an opt-in loopback address.

## Consequences

- Updating the table is a plane config change with a documented procedure; the shipped table holds
  vLLM 0.34.0/0.33.0 and SGLang 0.5.23/0.5.22 index digests.
- Engine panels and engine-metric autoscaling still read Dynamo series only; vLLM/SGLang series from
  container workers reach the store labelled by deployment id, ready for a later phase to chart.
