# 0029: Workload and Endpoint manifests are applied with PUT

**Status:** accepted · 2026-11-27 · builds on [0022](0022-plane-kinds-are-manifests-beside-the-http-api.md), [0025](0025-scale-is-spec-with-a-scale-subresource.md), [0026](0026-endpoint-targets-can-reference-a-workload.md)

## Context

Manifests could be read but not written. A declarative workflow (a file in git, `wings apply`,
later a GitOps controller) needs one idempotent write per object that creates it or brings it to
what the file says, with Kubernetes semantics people already know.

## Decision

- **`PUT /v1/manifests/{workloads|endpoints}/{project}/{name}`** creates (201) or replaces (200)
  the object and answers with its manifest. `metadata.name` must equal the path's name and
  `metadata.namespace`, when given, the path's project; otherwise 422.
- **Replace diffs declared fields only.** A field the manifest leaves out keeps its stored value,
  so server defaults never read as drift. A declared field equal to what is stored is not sent on,
  and the services' own spec compare ([0021](0021-workloads-and-endpoints-carry-a-spec-generation.md))
  decides the generation, so replaying a manifest moves no generation and rolls no deployment.
  Create-only fields (`enabled`) are ignored on replace until the last-applied record (P1.7).
- **Optimistic concurrency like `resourceVersion`.** `If-Match`, or `metadata.resourceVersion` in
  the body, makes the PUT conditional: a mismatch is 409. Neither is last-write-wins.
- **Immutable fields** (`x-immutable`: `kind`, `gangSize`, `cluster`, `placement.allowedRegions`,
  `placement.allowedClusters`, plus `serving.artifact`) changed on replace are a 422 naming the field.
- **Scale and placement shortfalls are status** on PUT, through `change_scale(refuse=False)`
  ([0025](0025-scale-is-spec-with-a-scale-subresource.md)); `PATCH` and operations keep refusing.
  `spec.replicas` omitted leaves `desired_scale` alone, which is the hook an autoscaler needs.
- **References.** Artifacts are never created implicitly: a missing artifact, or another project's
  private one, is a 422. An endpoint may name a workload that does not exist yet (a `ResolvedRefs`
  condition, [0026](0026-endpoint-targets-can-reference-a-workload.md)); deployment targets accept
  the deployment's object name, which is what a manifest read writes. A slug another endpoint holds
  is 409. A workload whose deployments the plane only observes is 409 until adopted.
- **`wings apply -f`** PUTs every Workload, then every Endpoint, in a file. Artifact manifests are
  refused for now.

## Consequences

A workload replace locks the workload row, checks `If-Match` under that lock, and commits the spec
update, pause and scale in one transaction, so it applies whole or not at all. A create with
`paused: true` is paused in the create's own transaction. Conditions keep `lastTransitionTime` null: they are derived on read and no transition
history is stored yet. Pruning, field ownership and `wings diff` wait for the last-applied record.
