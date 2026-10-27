# Roadmap

What Hangar does not do yet. Everything here is either reserved in the object model or designed in
outline, and none of it works today. The [README](../README.md) and the
[architecture overview](architecture/README.md) describe only what is built; when something on this
list ships, it moves there.

There are no dates. Order within a section is roughly the order the work is expected in.

## Workloads

- **Task and gang workloads.** A Workload carries a `kind`, and the object model reserves `task`
  (batch work that runs to completion) and `gang` (N devices together or none, for training and
  multi-node jobs) beside `service`. The plane refuses `task` and `gang` workloads at creation:
  the operator turns a deployment into an NVIDIA Dynamo graph and has no renderer for a Job, JobSet
  or other batch object. Queueing
  policy, deadlines, checkpointing and elastic resize all depend on this and are not started.
- **Preemption.** PriorityClass carries a `preemptible` flag, but nothing evicts anything and the
  `preemption` field in placement answers is always null. Placement counts only free devices as
  capacity, so a workload is only placed where devices are free today.
- **Quota floors and fair share.** A Quota records a guaranteed floor, a limit and an over-quota
  weight, but placement uses it only as a ceiling on what the project may hold. The floor is not
  reserved against other projects, and idle capacity is not shared out by weight.
- **Engines other than NVIDIA Dynamo.** The wire carries Hangar's own objects so a second renderer
  is possible, but Dynamo (with vLLM, SGLang or TensorRT-LLM inside it) is the only one.
- **Endpoint-level benchmarks.** Harnesses declare an `endpoint` level (conformance with its
  failover probe, evalscope against a served endpoint, an LLM judge credential), but nothing
  executes a run at that level. The plane serves only the `deployment` and `workload` levels and
  refuses an endpoint-level request until an executor that drives traffic through the gateway
  exists.

## Serving

- **Autoscaling.** A workload declares `min`, `max` and `desired` scale, and placement renders a
  fixed replica count per cluster from them. The maximum is reserved for an autoscaler and nothing
  reads it. Where the scaling decision is made (near the gateway, where the traffic signal is, or in
  the plane, where the bounds are) is an open design question.
- **Multi-cluster failover.** An Endpoint can resolve to deployments on several clusters. The
  gateway either splits traffic by weight or sends it to the first live upstream in a declared
  order, where live means the gateway holds a tunnel to that cluster, and it prefers deployments
  that report ready. Failure detection from request errors or latency, automatic cutover, and moving traffic
  away from a degrading cluster are not built.
- **ServingQuota enforcement.** Committed request rate, token rate and concurrency per endpoint can
  be declared and are shown in the console, but the gateway does not count or refuse traffic
  against them yet.
- **Regional gateways.** The gateway is one deployment, so it is one failure domain for inference
  everywhere. Running a gateway per region or per cluster, with the routing table scoped to it, is
  not designed yet.
- **Gateway cold start without the plane.** A running gateway keeps serving from its last routing
  table when the plane is down, but a gateway that restarts during a plane outage cannot load a
  table and does not start. Persisting the last known table would remove that dependency.
- **Gateway replica count at runtime.** Each gateway replica is told how many replicas there are
  with `HANGAR_GATEWAY_REPLICAS`, which has to match the StatefulSet by hand (see the gateway's
  [scaling notes](../libs/gateway/README.md#scaling)). Carrying the count in the tunnel listener
  config, or having agents dial each replica through a headless Service, would remove the setting.

## Security and access

- **Roles and scopes.** Human access is one admin role. Per-user accounts, roles, scoped API tokens
  and SSO are not built.
- **Authenticated metrics labels.** A label-enforcing proxy in front of `remote_write`, so a
  cluster can only write series under its own `cluster` label
  ([ADR 0012](decisions/0012-the-plane-is-the-source-of-the-metrics-write-htpasswd.md)).
- **Per-purpose cluster credentials.** One enrolment token authenticates a cluster's operator to
  the hub, its agent to the gateway and, by default, its metrics agent to `remote_write`. Separate
  credentials per component would keep a leaked metrics credential from opening a hub session.
- **Refresh token in an HttpOnly cookie.** The console keeps its refresh token in session storage,
  where script on the page can read it. Moving it to an `HttpOnly`, `SameSite=Strict` cookie scoped
  to `/v1/auth` needs the plane to set and read the cookie and the console to send credentials on
  refresh, together.
- **Enrolment token rotation.** Tokens are minted per cluster, but there is no rotation flow with
  an overlap window, so replacing one means minting a new token and running `helm upgrade` on that
  cluster.

## Packaging and operations

- **A Helm chart for the control plane.** Today the plane ships as images you run yourself; see
  [deploying the plane](operations/deploy-plane.md). The `cluster-setup` chart covers the
  cluster side only.
- **Metrics from the hub and reactor.** Only the API process serves `/metrics` (see
  [METRICS.md](../libs/plane/METRICS.md)); the hub and reactor processes expose none yet.
- **Published images and charts.** Nothing is published yet. Build the images into your own
  registry.
- **CRD versioning and conversion.** The CRDs are `v1alpha1` in the interim `hangar.dev` group, with
  no conversion webhooks. Upgrades between versions may require deleting and recreating them until
  a stable version exists.
- **Faster role and shape edits.** Changing a disaggregated workload's roles or device shape
  restarts every worker; see the [known limitations](architecture/README.md#14-known-limitations).
