# 0019: An artifact is named by a handle, not its slug

**Status:** accepted · 2026-11-26

## Context

A declarative API needs one name per object (`metadata.name`). Artifacts had three candidates and
none worked: `uid` is minted by the plane, `name` is display text that quantizations share, and
`slug` collides by construction. One Hugging Face repo at two commits is two artifacts with one
slug, and a discovery draft (`hf:Org/Model`, unverified) shares its slug with the artifact
registered later. The gateway routes on the slug, so making it unique would break that.

## Decision

Add `Artifact.handle`: unique within the artifact's scope, immutable, DNS-subdomain shaped (lowercase
alphanumerics, `-`, `.`, at most 253 characters) so it can become a Kubernetes name unchanged.

- Declared on create, or derived from the slug (else the name) with `/` and other separators
  folded to `-`. When the derived handle is taken, a suffix from the sha256 of the digest is added;
  the digest is unique within the scope, so the suffix is deterministic.
- There is no update field. A different handle is a different reference, and references in
  manifests must not move under them.
- Scoped by `Artifact.owner_project_id`. NULL is the public catalogue, shared by every project;
  set, the artifact is private to that project (a private Hugging Face repo, a fine-tune, a LoRA).
  Two partial unique indexes enforce each of handle and digest: one over public rows, one over
  `(owner_project_id, …)` for private rows. Digest dedup therefore stays inside a scope: a private
  copy of a public model is its own row, and two projects never share a private one. `project_id`
  stays what it was, who registered the artifact, and scopes nothing.
- References resolve by id, then handle, then name, among the public artifacts and the caller's
  project's private ones, private first. No reference by handle or name reaches another project's
  private artifact. Reads by id, listings, usage and lineage counts, and every join from a
  workload, endpoint, benchmark, metric, event, operation or desired-state frame are scoped the same
  way, through one predicate (`artifacts/scope.py:visible`); out of scope reads as not found.
  Per-project credentials for pulling private weights are a later phase.
- The migration backfills oldest first, so the first artifact registered keeps the plain handle.

## Consequences

The slug stays what the gateway routes on and may still collide. A handle alone no longer names
one artifact across the plane: a manifest's artifact reference is read in the manifest's project. A handle says nothing about the
runtime that serves the artifact, so nothing here assumes Dynamo or any one worker.
