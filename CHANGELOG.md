# Changelog

All notable changes to Wingsmith are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, APIs, CRDs and the wire
protocol can change between minor versions.

## [Unreleased]

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
