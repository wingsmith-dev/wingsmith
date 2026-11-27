# 0025: Scale is spec, written through a `/scale` subresource

**Status:** accepted · 2026-11-27 · amends [architecture §6.3](../architecture/README.md) ("Scaling is an operation, not a field write")

## Context

Scale was only reachable through the `workload.scale` operation, because a scale re-expands the
workload across clusters and can sit short on capacity indefinitely. A declarative apply
([0022](0022-plane-kinds-are-manifests-beside-the-http-api.md)) needs the replica count to be a field
it can declare and diff, and Kubernetes users expect `spec.replicas` and a `/scale` subresource.

## Decision

- **`spec.replicas` is the workload's `desired_scale`.** It is spec: writing it bumps `generation`
  ([0021](0021-workloads-and-endpoints-carry-a-spec-generation.md)) and is checked against `If-Match`.
  `spec.scale.min` and `spec.scale.max` stay as the bounds. `spec.scale.desired` is a read-only alias
  of `spec.replicas` in `v1alpha1` and is dropped before beta; an apply never reads it.
- **`GET`/`PUT /v1/manifests/workloads/{project}/{name}/scale`** read and write a `Scale`
  (`spec.replicas`, `status.replicas` = what placement placed). A PUT calls the same scale change the
  operation does and files a succeeded `workload.scale` Operation in the same commit, so the audit
  trail is unchanged. A stale `If-Match` or `metadata.resourceVersion` is a 409. Restating the
  current count is a no-op: no generation, no operation.
- **Unschedulable is status, not an error, on the declarative path.** A PUT keeps the new count and
  whatever placement could place; the shortfall reads as an `Unschedulable` condition on the
  workload manifest's status, derived from the scale rollup (placed below desired, not paused).
  A count outside `spec.scale.min`/`max` widens the bounds to it while no autoscaler exists, the
  way `kubectl scale` ignores HPA bounds; revisit when the autoscaler lands. Counts that contradict
  the kind (gang sizes) are still refused (422). `PATCH /v1/workloads` and
  the operations keep refusing as before.
- **An autoscaler owns the count when it is on.** Like a Kubernetes HPA, once the plane autoscaler
  exists (it does not yet) a PUT to `/scale` on an autoscaled workload is refused with 409, and an
  apply that omits `spec.replicas` leaves `desired_scale` where the autoscaler put it.

## Consequences

The operation stays the way to change bounds or to get a refusal when capacity is short. Scale
writes now race other spec writes on `generation`, which is what `If-Match` is for.
