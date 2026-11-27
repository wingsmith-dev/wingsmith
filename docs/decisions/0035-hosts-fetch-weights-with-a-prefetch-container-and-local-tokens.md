# 0035: Hosts fetch weights with a prefetch container and read tokens from local files

**Status:** accepted · 2026-11-27

## Context

ADR 0034 let hosts serve, with weights put in the cache by hand. A cluster fetches with a prefetch
Job running the engine image against the shared volume, gated on the warm set, and a private
artifact is fetched with its project's token from an admin-applied Secret (ADR 0023). A host needs
the same without Kubernetes, and without the token value ever crossing the plane.

## Decision

- **A short-lived container per artifact** (`ws-prefetch-<artifact-id>`) runs the same python as the
  Job (moved to `internal/fetchscript`, shared by both), in `prefetch_image` or the image of a
  deployment serving the artifact, under the image allowlist. Only the artifact's cache subtree is
  bind-mounted, read-write; serving containers keep mounting it read-only. Concurrency is bounded
  (`max_prefetches`).
- **Residency is a completion marker**, written atomically by the host under
  `<cache>/.wingsmith/artifacts/` after the container exits 0 and left the directory non-empty.
  No container mounts that directory, so a download cannot mark itself complete, and a non-empty
  directory alone (an interrupted download, or one copied by hand) is never served. Residency is
  reported as `cache.observed`, as the operator does.
- **Recovery is collection.** Each pass inspects prefetch containers; one that finished while the
  host was down is recorded then. A Hugging Face download resumes from its partial files; any
  other download is restarted into an emptied directory. Failures back off from 1 to 30 minutes, in
  memory.
- **Tokens are local files** in `/etc/wingsmith/credentials`: `<project>.token` for a private
  artifact whose source names a credential, `_default.token` optionally for public ones, mode 0600
  or refused. A missing project file refuses the fetch with `CredentialUnavailable`. This mirrors
  the cluster, where an admin applies the project Secret and the plane sends only its name. The
  value reaches only the prefetch container's environment.
- **Eviction is watermark-driven LRU** over marked artifacts outside the warm set and the declared
  deployments' artifacts, skipping any path a replica container mounts (read from a new
  `wingsmith.dev/model-path` label; an unlabelled replica disables eviction). Hugging Face repos
  sharing blobs are removed whole only when nothing else uses them.

## Consequences

- Hand-populated caches from P2.4 are re-verified by one prefetch run before serving resumes.
- The token sits in the prefetch container's config while it runs, readable by anyone with the
  Docker socket, who is root-equivalent on the host anyway.
- Per-project quotas and digest verification are not done, as on clusters.
