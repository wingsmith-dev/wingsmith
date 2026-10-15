# Changelog

All notable changes to Hangar are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions will follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once the first release is tagged. Until
then, APIs, CRDs and the wire protocol can change without notice.

## [Unreleased]

The first public version of the repository. Nothing has been tagged or published yet.

### Added

- The control plane (`libs/plane`): HTTP API, hub and reactor over one Postgres database.
- The per-cluster operator (`libs/operator`), which renders serving workloads through NVIDIA Dynamo.
- The gateway (`libs/gateway`), the agent (`libs/agent`) and the reverse tunnel between them
  (`libs/packages/tunnel`).
- The web console (`libs/console`) and the `hgr` command line (`libs/cli`).
- The `cluster-setup` Helm chart (`libs/chart`).
- The measurement tools: `hgrbench` (`libs/benchmarking`), `hgrtransit` (`libs/transit`) and
  `hgrconform` (`libs/conformance`).
- Architecture overview, decision records, roadmap, glossary and the plane deployment guide under
  `docs/`.
- Contributing guide with DCO sign-off, security policy, code of conduct and trademark policy.
