# 0026: Endpoint targets can reference a workload

**Status:** accepted · 2026-11-27

## Context

An endpoint target names one deployment, which is one workload on one cluster. When a workload
moves or spreads to another cluster, its new deployment is not routed to until somebody
re-points the endpoint. The declarative API wants an endpoint manifest that says "route to this
workload" and keeps meaning that, and one that can be applied before the workload exists.

## Decision

- **A target is a deployment or a workload ref, exactly one.** `targets: [{workload_ref: {name},
  weight}]` on the JSON API, and as `workloadRef` in `spec.targets` of the Endpoint manifest (ADR 0028). Stored as
  `workload_name` on `demand_endpoint_target` with a check constraint that exactly one of
  `deployment_id` and `workload_name` is set. Deployment targets are unchanged.
- **Same project only.** The ref is a name, resolved inside the endpoint's own project, like a
  same-namespace object ref. A workload of that name in another project never resolves.
- **Resolved at read time.** The routing table joins the ref to every current deployment of the
  workload, so a new deployment is routed to on the next poll and a removed one drops out. The
  usual filters (disabled target or deployment, non-serving cluster, hidden artifact) apply per
  deployment.
- **Weight is split evenly** over the deployments the ref produced after those filters: an integer
  split, the remainder one each to the first deployments by id, so the parts sum to the declared
  weight. Each part is floored at 1, because the gateway lifts a 0 weight to 1: a weight below the
  deployment count would otherwise get a larger share than declared without the plane seeing it. Such
  a ref's parts sum to the deployment count instead.
- **Unresolved is a condition, not an error.** A ref to a workload that does not exist is accepted.
  The endpoint reads with `ResolvedRefs` `False`, reason `WorkloadNotFound`, on the JSON API
  (`conditions`) and in the manifest's `status.conditions`, and it routes as soon as the workload
  appears. Deleting the workload leaves the ref in place and unresolved; it is never cascaded.

## Consequences

- A ref target cannot appear in `failover_order`, which orders deployments.
- The routing table and `GET /v1/endpoints?workload=` follow refs. Other rollups that join targets
  to deployments (endpoint readiness on `/v1/serving`, serving quota and benchmark joins, endpoint
  events) still read deployment targets only, and pick up refs when they move onto the resolved
  join.
- The default slug of an endpoint whose first target is a ref is the workload's artifact slug when
  the workload exists at create, else the endpoint name.
