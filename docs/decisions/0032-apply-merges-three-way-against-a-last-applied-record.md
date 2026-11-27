# 0032: Apply merges three-way against a last-applied record

**Status:** accepted · 2026-11-27 · builds on [0029](0029-manifests-are-applied-with-put.md)

## Context

A PUT that compares declared fields only (ADR 0029) cannot tell "the file no longer says this"
from "the file never said this", so deleting a line from a manifest and re-applying changed
nothing. `wings diff` also needs to know what an apply would do without doing it.

## Decision

- **A last-applied record.** Workloads and Endpoints keep `last_applied`, the body the last PUT
  sent (status and read-only metadata dropped), read back as
  `metadata.annotations["plane.wingsmith.dev/last-applied"]`, compact JSON, like
  `kubectl.kubernetes.io/last-applied-configuration`. It is not spec: writing it moves no
  generation and is outside the declared-field diff. It is stored in the PUT's own transaction.
- **Three-way merge on the server, one manager.** A field in the record and absent from the new
  body is reset to its schema default, as if declared; an `x-defaulted` one keeps the stored value
  (it is the plane's to fill), and an `x-immutable` one is a 422 naming it. Maps (labels,
  overrides, engine args) are user data and replaced whole. Fields never declared keep their
  stored value. No client-side merge.
- **No record, no change.** An object never applied (created over the HTTP API, or before this)
  keeps ADR 0029's declared-only behaviour; its first PUT starts the record.
- **`spec.replicas` is exempt while an autoscaler owns it** (the HPA rule). No autoscaler exists
  yet, so the hook (`autoscaled`) answers no.
- **`?dryRun=All`** runs the same path, reads the would-be object, rolls back and announces
  nothing.
- **`X-Wingsmith-Result`** is `created`, `configured` or `unchanged` (generation, labels and
  annotations other than the record all equal). `wings apply` prints it per object.
- **`wings apply -f file|dir|- [-R]`** applies Workloads, then Endpoints, then in file order; a
  failed document does not stop the rest, every result is printed and the exit is 1 if any
  failed. `--if-match` makes a single-document apply conditional, as `metadata.resourceVersion`
  already does; neither is sent by default.
- **`wings diff`** GETs each object, PUTs it with `dryRun=All` and prints a unified YAML diff of
  spec and metadata without the managed keys; exit 0 none, 1 some, above 1 on error.

## Consequences

Removing a field from a file now unsets it, which is what people coming from `kubectl apply`
expect. Pruning objects removed from a file, per-manager field ownership (`managedFields`),
`--server-side`, `--force-conflicts` and `--field-manager` are P1.12; they widen the record
without changing this API.
