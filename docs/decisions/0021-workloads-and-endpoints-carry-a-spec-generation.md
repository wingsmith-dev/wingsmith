# 0021: Workloads and endpoints carry a spec generation

**Status:** accepted · 2026-11-26

## Context

A declared object's `revision` was its last-write stamp. Ingestion and rollups write the same rows,
so `If-Match` would 409 on observation noise, and a GitOps controller re-syncing an unchanged
manifest could not tell "nothing changed" from "something did". Workload `PATCH` also rolled every
deployment whenever `overrides`, `adapter_artifacts` or `labels` appeared in the body, changed or
not.

## Decision

`Workload.generation` and `Endpoint.generation` count changes to the declared spec, start at 1 and
are moved only by spec writers. `revision` is `str(generation)` for both.

- Workload spec is every declared column plus the disaggregated roles; status is a short explicit
  list (`imported_args`, `started_at`, `finished_at`, stamps, ids). A new column counts as spec by
  default. Scale and pause are spec.
- `serving_digest` is not what the comparison reads. It is derived from columns already compared,
  and its construction will change (custom workers); the generation must not depend on it.
- Endpoint spec is the declared fields, the alias set, and the declared target set with its
  failover order. A `PATCH` that replaces the targets with a different set bumps; one that restates
  the same set does not. What targets resolve to at read time (drains, disabled clusters) never
  counts, since nothing wrote the endpoint.
- A patch whose values match what is stored bumps nothing: no generation, no deployment rollout, no
  `updated` event. Deployments still roll only when something a cluster renders moved.

`Deployment.generation`/`observed_generation` stay the plane-owned rollout counter.

## Consequences

`If-Match` protects the spec, not every column: a note on a workload does not invalidate a
held revision, a retarget does. Clients that parsed the old microsecond revision as
a time (none should) see small integers. Artifacts have no generation yet.
