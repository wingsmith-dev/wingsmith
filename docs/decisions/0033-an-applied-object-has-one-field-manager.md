# 0033: An applied object has one field manager

**Status:** accepted · 2026-11-27 · builds on [0032](0032-apply-merges-three-way-against-a-last-applied-record.md)

## Context

With a last-applied record (ADR 0032) a manifest owns the object it declares, but the console and
the HTTP API could still change it underneath. The next apply then silently reverted the change, or
merged against a record that no longer described the object. Two pipelines applying the same object
had the same problem.

## Decision

- **Who applied it.** Workloads and Endpoints carry `applied_by` (the field manager),
  `applied_by_principal` and `applied_at`, written by the PUT that writes `last_applied`. The
  manager comes from `?fieldManager=` (default `wings-cli`, at most 128 characters, as in
  Kubernetes); the principal is the caller. Objects applied before this were applied by `wings apply`,
  so the migration sets `applied_by = 'wings-cli'` wherever `last_applied` is set.
- **Read-only on the wire.** The manifest GET shows them as
  `metadata.annotations["plane.wingsmith.dev/applied-by"]`, `.../applied-by-principal` and
  `.../applied-at`; the JSON API as `applied_by`, `applied_by_principal`, `applied_at`. A PUT body
  carrying these annotations has them dropped, like the record. `app.kubernetes.io/managed-by` is
  left alone: labels are the user's spec.
- **The guard.** While `applied_by` is set, a `PATCH /v1/workloads/{ref}`, a
  `PATCH /v1/endpoints/{ref}`, a workload operation (`POST /v1/operations`), or a PUT naming another
  manager is a 409 whose message and `detail.manager` name the manager. `workload.drain` stays
  allowed: it moves no spec. A PUT from the same manager, or onto an object nobody manages, passes.
- **Taking over.** `?force=true` on any of those clears `applied_by`, the principal, the time and
  `last_applied`, then makes the change. The takeover files a succeeded `workload.takeover` or
  `endpoint.takeover` operation (who, from which manager, to which) in the same transaction. A
  forced PUT then records its own manager and, with no record to merge against, compares declared
  fields only.
- **Observed workloads.** A PUT onto a workload with observed deployments is a 422 ("observed,
  adopt it first"), not a 409: no amount of retrying or forcing makes it applicable.
- `wings apply --field-manager <name>` and `--force-conflicts` send the two parameters; the
  console shows "Managed by <manager>" on workload and endpoint pages, disables their edit
  controls, and offers "Take over" behind a confirm step.

## Consequences

The console can no longer drift a manifest-managed object by accident. One manager owns the whole
object; scale and pause under an autoscaler, and per-field managers (`managedFields`), are P1.12,
which widens `applied_by` into a field set without changing this API.
