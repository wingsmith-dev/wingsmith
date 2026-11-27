# 0042: Workload CRDs in member clusters, synced to the plane by the operator

**Status:** accepted · 2026-11-27

## Context

Teams want to declare Wingsmith workloads with the tools they already run against their clusters:
kubectl, Argo CD, Flux. Serving a Kubernetes API from the plane was dropped (P1.11 redirect). The
manifests `wings apply` sends are already Kubernetes-shaped (`plane.wingsmith.dev/v1alpha1`,
spec/status, field managers, three-way apply), so the same documents become custom resources in a
member cluster and the operator, which already holds a session to the plane, syncs them.

## Decision

- **CRD `workloads.plane.wingsmith.dev`, v1alpha1, namespaced, status subresource.** Installed by
  the cluster-setup chart. The schema is hand-written and loose (`x-kubernetes-preserve-unknown-fields`
  under spec); the plane validates fully and reports refusals in status. Generated schema with CEL
  for `x-immutable` comes later (P1.17).
- **Namespace to project.** A namespace labelled `plane.wingsmith.dev/project=<slug>` declares into
  that project; `cluster.defaultProject` in the chart covers unlabelled namespaces. Other
  namespaces are not synced.
- **Wire.** `manifest.apply {kind, project, name, namespace, uid, body, generation}` and
  `manifest.delete`, answered by `manifest.result {result, generation, uid, rv, plane_generation,
  manager, allowed_clusters, error}` replying to the request id. The hub calls
  `ManifestService.put_workload` / `delete_workload` directly.
- **Field manager `k8s:<cluster>/<namespace>`**, derived by the hub from the authenticated cluster
  and the frame's namespace, never read from the frame. The HTTP `fieldManager` grammar has no `:`
  or `/`, so no HTTP client can apply as a cluster. A second namespace declaring the same name in
  the same project is a different manager and gets a conflict.
- **Scoping.** A cluster applies only into projects linked to it: `supply_cluster_project`,
  managed by `PUT|DELETE /v1/clusters/{ref}/projects/{project}` and listed by `GET
  /v1/clusters/{ref}/projects`. Each link carries an explicit allowlist of the cluster namespaces
  that may declare into the project (`namespaces`, set by the PUT body, replaced whole); an empty
  list allows none, and links that predate it start empty. A namespace label alone is not enough,
  since anyone who can label a namespace could otherwise write into any linked project. Anything
  else is answered `forbidden`, with `reason: NamespaceNotAllowed` for an unlisted namespace.
- **Origin.** The hub stamps `plane.wingsmith.dev/origin-cluster`, `origin-namespace` and
  `origin-uid` into the applied body, so they live in the object's last-applied record and the
  objects a removed cluster leaves behind can be found by querying it.
- **Placement defaults to this cluster only.** When the resource sets none of `spec.cluster`,
  `spec.placement.allowedClusters` or `allowedRegions`, the operator sends
  `allowedClusters: [<this cluster>]` on the wire. The resource's spec is never written, so GitOps
  tools see no drift; the effective list is echoed in `status.placement`. Spreading is opt-in by
  listing clusters.
- **`allowedClusters` is mutable** (was `x-immutable`). A change rewrites the workload's generated
  placement policy, bumps the spec generation, and takes effect at the next expansion, like
  `strategy`; running deployments are not moved by the write. No schema migration: the policy row
  already existed. `allowedRegions` stays immutable.
- **Deletion.** Finalizer `plane.wingsmith.dev/sync`. `spec.deletionPolicy: Delete|Orphan`
  (default Delete, Karmada/OCM style, stripped from the body sent to the plane); Orphan releases
  without deleting. The operator sends `manifest.delete`; the
  plane deletes the object if this manager applied it, otherwise releases it (`released`, left in
  place). `absent` and `forbidden` also release the finalizer.
- **Status.** The operator writes only `.status` and `metadata.finalizers`, as merge patches:
  `observedGeneration`, `planeUID`, `planeResourceVersion`, `planeGeneration`, `fieldManager`,
  `placement`, and conditions `Synced` (acceptance: True `Applied`; False `ManagerConflict`,
  `Observed`, `Invalid`, `Forbidden`, `PlaneError`; Unknown `PlaneUnreachable`), `Ready` (serving;
  `Unknown/AwaitingStatus` until P1.16 brings serving status from the plane watch), and the kstatus
  pair `Stalled` (terminal refusal) / `Reconciling` (waiting on the plane or on serving status). No
  separate `Conflict` condition: `Synced=False/ManagerConflict` is it. Printer columns Synced,
  Ready, Age. An Argo CD Lua health check ships in `libs/operator/deploy/argocd/`; a fresh resource
  without status reads Current to kstatus.
- **Offline and reconnect.** Requests are not queued while the hub session is down: the resource
  goes `Synced=Unknown/PlaneUnreachable` and is retried every 30s. Every reconnect relists and
  reapplies every resource; the plane's server-side diff answers `unchanged`, so replays move
  nothing.

## Consequences

- One manifest file works for `wings apply` and `kubectl apply`.
- A resource stuck Terminating while the plane is unreachable can be let go by removing the
  finalizer by hand; the plane object is then left behind and found by its origin annotations.
- The loose schema (`x-kubernetes-preserve-unknown-fields`) means typos surface as
  `Synced=False/Invalid` after admission, not as an API-server refusal, until P1.17.
- Deferred: Endpoint CRD and status from the plane watch (P1.16), generated schema/CEL, force
  annotation (P1.17), Artifact CRD and per-field ownership (P1.18). Hosts keep `wings apply`.
