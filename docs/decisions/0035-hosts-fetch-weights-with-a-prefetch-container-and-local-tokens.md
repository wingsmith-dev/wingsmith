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
  deployment serving the artifact, under the image allowlist. Only the artifact's own directory is
  bind-mounted, read-write: for Hugging Face the repo's `hub/models--<org>--<repo>` and its
  `hub/.locks/` entry, never HF_HOME or the project directory, so one download cannot rewrite
  another artifact's blobs (the xet cache goes to the container's tmpfs). Serving containers keep
  mounting the cache read-only. Concurrency is bounded
  (`max_prefetches`).
- **Residency is a completion marker**, written atomically by the host under
  `<cache>/.wingsmith/artifacts/` after the container exits 0 and left the directory non-empty.
  No container mounts that directory, so a download cannot mark itself complete, and a non-empty
  directory alone is never served unless adopted (below). Residency is
  reported as `cache.observed`, as the operator does.
- **Recovery is collection.** Each pass inspects prefetch containers; one that finished while the
  host was down is recorded then. A Hugging Face download resumes from its partial files. Any
  other download leaves a `<id>.fetching` breadcrumb beside the markers while it runs; only a
  directory with that breadcrumb is the host's own partial and is emptied before a retry, and
  never while any replica's `wingsmith.dev/model-path` label points at it. A non-empty directory
  without breadcrumb or marker (a hand-filled cache, or one from before markers) is adopted: the
  host writes its marker, after checking it holds at least the artifact's declared size. Failures back off from 1 to 30 minutes, in
  memory.
- **Tokens are local files** in `/etc/wingsmith/credentials`: `<project>.token` for a private
  artifact whose source names a credential, `_default.token` optionally for public ones. The file
  is opened once without following symlinks and the open handle checked: a regular file, mode
  0600, owned by root, or refused. A missing project file refuses the fetch with `CredentialUnavailable`. This mirrors
  the cluster, where an admin applies the project Secret and the plane sends only its name. The
  value reaches only the prefetch container's environment.
- **Eviction is watermark-driven LRU** over marked artifacts outside the warm set and the declared
  deployments' artifacts, skipping any path a running or restarting replica mounts (read from a
  new `wingsmith.dev/model-path` label; a live unlabelled replica disables eviction). A stopped
  replica holds nothing; its weights are fetched again when it next starts. Hugging Face repos
  sharing blobs are removed whole only when nothing else uses them.

## Consequences

- Hand-populated caches from P2.4 are adopted as they are, not re-downloaded or deleted; their
  contents are trusted as far as the size check goes.
- The token sits in the prefetch container's config while it runs, readable by anyone with the
  Docker socket, who is root-equivalent on the host anyway.
- Per-project quotas and digest verification are not done, as on clusters.
