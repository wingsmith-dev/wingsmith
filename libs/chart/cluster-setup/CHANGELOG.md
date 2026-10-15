# Changelog

All notable changes to the cluster-setup chart. The chart follows
[Semantic Versioning](https://semver.org/); while it is below 1.0, a minor
version may break values or CRDs, and every such change is listed under
**Breaking** with what to do.

## 0.3.0

The first public release. Earlier versions were internal and are not supported
upgrade sources; install fresh.

### Breaking

- The operator's CRDs moved to the `hangar.dev` API group
  (`syncedresources.hangar.dev`, `streamcheckpoints.hangar.dev`). Helm does not
  install new CRDs on upgrade: apply `crds/` by hand first.
- `appVersion` is a release version, not `latest`. The operator and agent
  images default to it, with `pullPolicy: IfNotPresent`.
- `cluster.allowedOperations` defaults to empty, so a read-only cluster performs
  no operations until you name them.
- The operator's RBAC follows `cluster.readOnly` and
  `cluster.allowedOperations`: a read-only install gets no write access outside
  the release namespace.
- `cluster.hubUrl` must be `wss://`. A `ws://` URL is refused unless
  `hub.allowInsecure: true`, which also tells the operator to accept it; use it
  for a local development plane only. `hub.caBundle`,
  `agent.gateway.serverName` and `agent.gateway.caBundle` are new, for a plane
  whose certificates come from a private CA.
- Ingress NetworkPolicies for the operator, serving agent, inference
  frontends and NATS are on by default (`networkPolicy.enabled`). Anything
  else that must reach a frontend or NATS goes in `networkPolicy.frontendFrom`
  or `networkPolicy.natsFrom`.
- `serving.enabled` requires an authenticating Traefik Middleware in
  `serving.middlewares` (or `serving.allowUnauthenticated: true`), and, with
  network policy on, the ingress controller in `networkPolicy.frontendFrom`.
- `frontend.enabled` and `operator.frontend.enabled` are removed in favour of
  `frontend.mode`; the chart refuses either with migration instructions.
