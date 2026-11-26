# 0023: Private artifacts are fetched with a project token into a project cache

**Status:** accepted · 2026-11-26

## Context

A private artifact ([0019](0019-an-artifact-is-named-by-a-handle-not-its-slug.md)) belongs to one
project. Its weights may come from a private Hugging Face repo that needs that project's token, and
once fetched they sit on the cluster's shared model cache volume beside every other project's.
Until now every prefetch and serving pod read the cluster-wide `hf-token-secret` and mounted the
whole volume at `/models`.

## Decision

- **The token is a credential reference.** A project declares an `hf_token` credential with
  `PUT /v1/projects/{project}/credentials/{name}`: a row in `ops_credential` scoped to the project
  that holds only `secret_ref`. The value is never in the plane's database and never crosses the
  hub. `artifact.desired` carries `owner_project` and, on a private artifact's sources, the
  `secret_ref` of a usable credential of that same project (the one the location names, else the
  project's newest `hf_token`). Public artifacts carry no reference.
- **The cluster holds the value.** `secret_ref` names a Secret an admin applied in the operator's
  namespace, with the token under `HF_TOKEN` and the label `wingsmith.dev/project=<project>`. The
  operator copies it to `hf-<project>` in the workload namespace before the prefetch Job starts, and
  waits with `CredentialUnavailable` when the Secret is missing, unlabelled for the project, or
  empty. The label is the admin's grant: a project cannot name a Secret meant for anyone else. The
  Secret's name is a function of namespace and project, so per-project namespaces can come later.
  There is no external secret-store integration yet; an External Secrets or Vault controller that
  writes that Secret works unchanged.
- **Only the prefetch Job holds it.** A private artifact's prefetch Job reads `hf-<project>` and
  never the cluster-wide token. Its workers, frontend and benchmark Jobs get no token and run
  with `HF_HUB_OFFLINE=1`, reading what the prefetch put in the cache. Public artifacts keep the
  optional `hf-token-secret` everywhere, as before.
- **Private bytes live under the project.** The cache is laid out as `/models/hf` (public HF),
  `/models/<artifact-id>` (public, other sources) and `/models/p/<project>/{hf,<artifact-id>}`
  (private). Every pod mounts only the directory its artifact lives in, by `subPath`, at the same
  path, never the volume root; a benchmark mounts the public HF cache and, for a private target,
  its project's directory. A private copy of a public repo is fetched again: no dedup across scopes.

## Consequences

This isolates tenants who cannot schedule arbitrary pods in the shared workload namespace. It is
not isolation from anyone with cluster access: the volume, the Secrets and the namespace are
shared, and whoever can create a pod there can mount any `subPath` or read any Secret. Custom
workers (P8), where a tenant chooses the pod, need per-project namespaces first.

The operator now gets `get` on Secrets in its own namespace and `create`, `get`, `update` in the
workload namespace on a managed cluster, read directly and never listed or watched. Running
deployments keep their current render until their next change, since the reconciler compares
inputs and not rendered output. Resolving a private repo's metadata on the plane at artifact
registration still uses the plane's own token (`WINGSMITH_HF_TOKEN`).
