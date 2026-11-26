# 0019: An artifact is named by a handle, not its slug

**Status:** accepted · 2026-11-26

## Context

A declarative API needs one name per object (`metadata.name`). Artifacts had three candidates and
none worked: `uid` is minted by the plane, `name` is display text that quantizations share, and
`slug` collides by construction. One Hugging Face repo at two commits is two artifacts with one
slug, and a discovery draft (`hf:Org/Model`, unverified) shares its slug with the artifact
registered later. The gateway routes on the slug, so making it unique would break that.

## Decision

Add `Artifact.handle`: unique across the catalogue, immutable, DNS-subdomain shaped (lowercase
alphanumerics, `-`, `.`, at most 253 characters) so it can become a Kubernetes name unchanged.

- Declared on create, or derived from the slug (else the name) with `/` and other separators
  folded to `-`. When the derived handle is taken, a suffix from the sha256 of the digest is added;
  the digest is already unique, so the suffix is deterministic.
- There is no update field. A different handle is a different reference, and references in
  manifests must not move under them.
- Unique globally rather than per project: artifacts are a catalogue, and shared ones have no
  project.
- References resolve by id, then handle, then name.
- The migration backfills oldest first, so the first artifact registered keeps the plain handle.

## Consequences

The slug stays what the gateway routes on and may still collide. A handle says nothing about the
runtime that serves the artifact, so nothing here assumes Dynamo or any one worker.
