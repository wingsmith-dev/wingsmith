# 0031: Custom workers run as a container runtime beside Dynamo

**Status:** accepted · 2026-11-27

## Context

Every workload ran under NVIDIA Dynamo. People want to serve with their own image (a patched vLLM,
an in-house server) without Dynamo, and a Linux host (P2) has no Dynamo at all. P8.1 settled the
contract; this records the model decisions the MVP (P8.2) took.

## Decision

- **`runtime` is a tagged union on the workload.** `kind: dynamo` is the default and changes
  nothing for existing manifests or rows. `kind: container` carries image, command, args, env,
  ports, protocol, probes (`corev1.Probe`-shaped) and metrics. The plane stores it as
  `Workload.runtime_kind` plus `runtime_spec` JSONB, and writes `engine = 'container'` instead of
  relaxing the engine column. `runtime.kind` is immutable after create.
- **The digest covers the runtime only off Dynamo.** `serving_digest` adds `runtime_kind` and
  `runtime_spec` only when the kind is not `dynamo`, so every existing digest, deployment annotation
  and benchmark link is byte-identical.
- **Images are digest-pinned.** A tag is refused unless the admin flag
  `CONTAINER_IMAGE_TAGS_ALLOWED` is set, so a pushed tag cannot change what a cluster runs. The
  operator still enforces its `AllowedImagePrefixes`.
- **The platform owns the model location and the sandbox.** It mounts the artifact's cache
  directory read-only (the P1.4b subPath), sets `WS_MODEL_PATH`, `WS_SERVED_MODEL_NAME`, `WS_PORT`
  and `HF_HUB_OFFLINE=1`, and `args` reference them as `$(VAR)`. The pod runs non-root with every
  capability dropped, no privilege, no host network or hostPath, and no service-account token.
  Exec probes, overlays, engine args, router settings and disaggregated topology are refused.
- **On Kubernetes it is a second `WorkloadRenderer`.** `KubeBackend` picks Dynamo or container per
  deployment; the container renderer applies an `apps/v1` Deployment and a Service labelled
  `wingsmith.dev/runtime=container` and `wingsmith.dev/role=frontend`, observes the worker pods,
  prunes renamed objects and deletes by deployment id. The agent treats a `role=frontend` Service
  with a deployment id as that deployment's frontend.
- **Runtimes are discovered, and placement gates on them.** The operator reports `runtimes` on
  `cluster.observed` (`container`, plus `dynamo` when `DynamoGraphDeployment` is served). A cluster
  without Dynamo takes container workloads only; unknown runtimes read as Dynamo-capable.

## Consequences

- MVP limits: `openai-chat` only, aggregated, one node per replica, a pinned artifact (the worker
  runs offline). Completions, embeddings, plain HTTP, Secret env refs, presets, gang workers and
  host execution are P8.3/P8.4.
- A container workload has no Dynamo frontend, so engine-metric autoscaling and KV routing do not
  apply; gateway-side latency and token metrics still do.
