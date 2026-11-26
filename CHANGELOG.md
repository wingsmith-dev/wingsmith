# Changelog

All notable changes to Wingsmith are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, APIs, CRDs and the wire
protocol can change between minor versions.

## [Unreleased]

### Added

- Artifacts carry a unique, immutable `handle` (`openai-gpt-oss-20b`), derived from the slug or
  declared at create, and accepted wherever an artifact reference is. Existing artifacts are
  backfilled oldest first.
- Artifacts can be private to a project (`private: true` with `project` on create, read back as
  `owner_project_id`). Handle and digest are unique within the public catalogue or within one
  project, and a reference by handle or name reaches only public artifacts and the caller's own.
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

- The model cache keeps private artifacts under `/models/p/<project>/`, and serving, prefetch and
  benchmark pods mount only their artifact's directory by `subPath` instead of the volume root.
  Serving pods of a private artifact get no token and run with `HF_HUB_OFFLINE=1`. On a managed
  cluster the operator now reads Secrets in its namespace and writes them in the workload namespace.
- Workloads and endpoints carry a spec `generation`, and `revision` (what `If-Match` checks) is now
  that number instead of the last-write stamp, so observation no longer reads as a concurrent edit.
  A replayed workload `PATCH` that changes nothing rolls no deployment and records no event.
- Deployments render under a cluster-unique `object_name` (workload name plus the tail of the
  workload id) instead of the bare workload name, so workloads of one name in different projects
  can share a cluster. Existing and adopted deployments keep their current name.
- A bare deployment name in a lookup (`GET /deployments/{ref}`, metrics, benchmarks, operations)
  is read as the cluster object name; pass `project` to `GET /deployments/{ref}` to look one up
  by workload name. Endpoint targets resolve bare names inside the endpoint's own project.

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
