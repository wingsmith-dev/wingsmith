# Changelog

All notable changes to Wingsmith are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, APIs, CRDs and the wire
protocol can change between minor versions.

## [Unreleased]

### Added

- Ownership of applied objects. A PUT records its field manager (`?fieldManager=`, default
  `wings-cli`), principal and time on the Workload or Endpoint, shown as
  `plane.wingsmith.dev/applied-by*` annotations and `applied_by*` fields. A console PATCH, a workload
  operation other than drain, or a PUT from another manager is then a 409 naming the manager;
  `?force=true` takes the object over and files a `*.takeover` operation. `wings apply
  --field-manager` and `--force-conflicts`; the console shows "Managed by" with a "Take over" action
- Hosts serve container workloads (`runtime.kind: container`). With `serve: true` and a `gateway`
  in host.json, `wingsmith-host run` creates one Docker container per replica with GPUs pinned by
  UUID, the model cache mounted read-only, the port on 127.0.0.1 only, no capabilities and a
  read-only root; readiness runs the workload's httpGet probe; and the same binary holds the
  data-path tunnel. Placement now takes container workloads on hosts that report the runtime and
  still refuses Dynamo there. Weights must be in the cache by hand until host prefetch lands
  (ADR 0034).
- `wings apply -f file|dir|- [-R]` and `wings diff`. The plane keeps the last manifest applied to
  each Workload and Endpoint (`plane.wingsmith.dev/last-applied`) and merges against it, so a field
  removed from the file is reset on the next apply. PUT takes `?dryRun=All` and answers
  `X-Wingsmith-Result: created|configured|unchanged`; apply carries on past a failed document and
  exits 1, diff exits 0/1/>1 (ADR 0032).
- Custom workers: a workload can declare `spec.runtime.kind: container` (HTTP API
  `serving.runtime`) with a digest-pinned image, command, args, env, ports, probes and metrics. The
  operator renders a Deployment and Service with the model mounted read-only at `$WS_MODEL_PATH`;
  the agent routes to it by `wingsmith.dev/role=frontend`. MVP: `openai-chat`, aggregated, one node.
  Operators report discovered `runtimes`, and clusters without Dynamo take container workloads only.
  `wings workload create --runtime container --image ...` (ADR 0031).
- `PUT /v1/manifests/{workloads|endpoints}/{project}/{name}` creates (201) or replaces (200) an
  object from its manifest: only declared fields are compared, so a replay changes nothing;
  `If-Match`/`metadata.resourceVersion` conflicts are 409, immutable field changes 422; replica
  shortfalls are reported as `Unschedulable`. `wings apply -f file.yaml` applies Workloads, then
  Endpoints (ADR 0029). Endpoint deployment targets accept the deployment's object name.
- `wingsmith-host`: a bare Linux GPU machine enrols as a cluster of `kind: host` with one node and
  reports its GPUs from NVML (falling back to `nvidia-smi -q -x`). Supply only: placement refuses
  hosts with `cluster_kind` until they can serve. Clusters carry a declared `kind` (ADR 0030).
- Operator: a `Backend` seam (apply, observe, inventory) with Kubernetes/Dynamo as its
  implementation, and a file-backed resume cursor for the coming host agent (ADR 0027).
- Endpoint targets can name a workload in the endpoint's project (`workload_ref: {name}`), resolved
  at read time to its current deployments with the weight split evenly over them. A ref to a
  missing workload is accepted and reported as a `ResolvedRefs` condition (ADR 0026).
- Artifacts carry a unique, immutable `handle` (`openai-gpt-oss-20b`), derived from the slug or
  declared at create, and accepted wherever an artifact reference is. Existing artifacts are
  backfilled oldest first.
- Artifacts can be private to a project (`private: true` with `project` on create, read back as
  `owner_project_id`). Handle and digest are unique within the public catalogue or within one
  project, and a reference by handle or name reaches only public artifacts and the caller's own.
- Workload manifests carry `spec.replicas` (`spec.scale.desired` is now a read-only alias) and an
  `Unschedulable` status condition, and `GET`/`PUT /v1/manifests/workloads/{project}/{name}/scale`
  reads and sets the replica count: spec, generation-bumping, `If-Match`-checked and audited as a
  `workload.scale` operation; a capacity shortfall is accepted and reported (ADR 0025).
- Workloads, endpoints and artifacts read as `plane.wingsmith.dev/v1alpha1` manifests at
  `GET /v1/manifests/{kind}/{project}/{name}` (public artifacts at `/v1/manifests/artifacts/{name}`),
  and `wings get <kind> <name> -o yaml` prints one.
- Reads by id, `GET /v1/artifacts`, usage counts and every join from workloads, endpoints,
  benchmarks, metrics, events, operations and cluster desired state see only public artifacts and
  the named project's private ones; another project's private artifact is a 404.

- Projects declare credentials at `PUT /v1/projects/{project}/credentials/{name}` (`kind:
  hf_token`, `secret_ref` only). A private artifact is fetched with its project's token, which the
  operator copies from an admin-applied, project-labelled Secret into `hf-<project>` and mounts on
  the prefetch Job only. The token never reaches the plane's database or the hub (ADR 0023).

### Changed

- Host rollouts start the new container beside the old one when the box has the GPUs for it, and
  move the route only once the new one is ready; otherwise they replace in place as before. A
  paused deployment on a host stops its containers and frees their GPUs.
- A container worker's env names must be valid identifiers outside the reserved `WS_` namespace,
  checked by the plane and by hosts.
- A PUT onto an observed workload is a 422 ("observed, adopt it first"), not a 409 (ADR 0033).

- Manifests (`plane.wingsmith.dev/v1alpha1`) are camelCase in `spec` and `status` as well as
  `metadata` (`workloadRef`, `servedModelName`, `readyReplicas`), and conditions carry
  `lastTransitionTime`. The `/v1` JSON API stays snake_case (ADR 0028).

- An endpoint created without a slug whose default (the artifact slug, else the lowercased name)
  is already taken gets `<project>-<default>` instead of a 409; a declared slug that clashes is
  still refused (ADR 0024). Serving quota and endpoint benchmark creates take `project` for an
  endpoint name two projects share. A migration refuses existing aliases equal to another
  endpoint's slug.
- Endpoint names are unique per project instead of globally. A bare name two projects share needs
  `?project=` (or the id) on endpoint reads, writes, metrics and SLA; `wings` retries it in the
  default project. Endpoint slugs are now unique, and a slug another endpoint has as its slug or
  alias is refused with 409; the migration stops and lists any existing duplicates (ADR 0024).

- The model cache keeps private artifacts under `/models/p/<project>/`, and serving, prefetch and
  benchmark pods mount only their artifact's directory by `subPath` instead of the volume root.
  Serving pods of a private artifact get no token and run with `HF_HUB_OFFLINE=1`. On a managed
  cluster the operator now reads Secrets in its namespace and writes them in the workload namespace.
  Upgrading: a private artifact already cached under `/models/hf` or `/models/<id>` is re-fetched
  into `/models/p/<project>/`, and the old copy is orphaned; remove it by hand.
- Workloads and endpoints carry a spec `generation`, and `revision` (what `If-Match` checks) is now
  that number instead of the last-write stamp, so observation no longer reads as a concurrent edit.
  A replayed workload `PATCH` that changes nothing rolls no deployment and records no event.
- Deployments render under a cluster-unique `object_name` (workload name plus the tail of the
  workload id) instead of the bare workload name, so workloads of one name in different projects
  can share a cluster. Existing and adopted deployments keep their current name.
- A bare deployment name in a lookup (`GET /deployments/{ref}`, metrics, benchmarks, operations)
  is read as the cluster object name; pass `project` to `GET /deployments/{ref}` to look one up
  by workload name. Endpoint targets resolve bare names inside the endpoint's own project.

### Fixed

- Hosts sync the allocation map and declared-state checkpoint to disk before renaming them into
  place, so a power loss cannot leave an empty file.
- Endpoints whose targets are workload refs now report their replicas in the serving view, count
  as serving in the fleet summary, and show up in quota and benchmark reporting and the quota
  `?workload=` filter.
- A workload ref's weight split floors every part at 1, matching what the gateway routes (ADR 0026).
- A moved-aside default slug is capped at 255 characters, and concurrent creates defaulting to the
  same slug serialize so the second one moves aside instead of failing with a 409.

## [0.3.0] - 2026-11-26

The first tagged release. Images for the plane, console, operator, agent, gateway and demo, and the
`cluster-setup` chart, are published to GHCR.

### Added

- The control plane (`libs/plane`): HTTP API, hub and reactor over one Postgres database.
- The per-cluster operator (`libs/operator`), which renders serving workloads through NVIDIA Dynamo.
- The gateway (`libs/gateway`), the agent (`libs/agent`) and the reverse tunnel between them
  (`libs/packages/tunnel`).
- The web console (`libs/console`) and the `wings` command line (`libs/cli`).
- The `cluster-setup` Helm chart (`libs/chart`).
- The measurement tools: `wingsbench` (`libs/benchmarking`), `wingstransit` (`libs/transit`) and
  `wingsconform` (`libs/conformance`).
- Architecture overview, decision records, roadmap, glossary and the plane deployment guide under
  `docs/`.
- Contributing guide, security policy, code of conduct and trademark policy.
