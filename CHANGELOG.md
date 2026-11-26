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

### Changed

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
