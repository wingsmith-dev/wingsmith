# Declaring workloads from your cluster

A member cluster can declare Wingsmith workloads as Kubernetes resources. Apply a `Workload`
(`plane.wingsmith.dev/v1alpha1`) with kubectl, Argo CD or Flux; the Wingsmith operator in that
cluster applies it to the plane and reports back in the resource's status (ADR 0042). The document
is the same one `wings apply -f` takes.

## Set up

1. Install or upgrade the cluster-setup chart. It installs the `workloads.plane.wingsmith.dev` CRD,
   the operator RBAC for it, and two aggregated roles: `wingsmith-edit` (folded into the built-in
   `edit`/`admin`) and `wingsmith-view` (folded into `view`). Helm does not upgrade CRDs in
   `crds/`; on an existing install apply `crds/plane.wingsmith.dev_workloads.yaml` once and restart
   the operator. `cluster.declareWorkloads: false` turns the feature off.
2. Link the project to the cluster on the plane. A cluster can only declare into linked projects:

   ```sh
   curl -X PUT -H "Authorization: Bearer $TOKEN" https://plane.example.com/v1/clusters/alpha/projects/search
   ```

3. Label the namespaces that declare into it:

   ```sh
   kubectl label namespace team-a plane.wingsmith.dev/project=search
   ```

   Or set `cluster.defaultProject` in the chart for namespaces without the label. Workloads in any
   other namespace are ignored.

## Apply

```yaml
apiVersion: plane.wingsmith.dev/v1alpha1
kind: Workload
metadata:
  name: chat
  namespace: team-a
spec:
  runtime: {engine: vllm, version: 0.6.0}
  serving:
    artifact: gpt-oss
    contextLength: 8192
    shape: {deviceClass: h100}
  replicas: 2
```

```sh
kubectl apply -f chat.yaml
kubectl get workloads -n team-a
kubectl wait workload/chat -n team-a --for=condition=Synced
```

The plane object is `search/chat`, managed by `k8s:<cluster>/team-a`. Another namespace or a
`wings apply` declaring the same name gets a conflict instead of overwriting it.

**Placement** defaults to this cluster only. To let placement use others, list them:
`spec.placement.allowedClusters: [alpha, beta]`. The list can change later; it takes effect at the
next expansion. The resource's spec is never rewritten; the effective list is in
`status.placement.allowedClusters`.

## Status

| Field | Meaning |
|---|---|
| `observedGeneration` | the resource generation the plane last answered for |
| `planeUID`, `planeResourceVersion`, `planeGeneration` | the plane object as last applied |
| `Synced` | `True/Applied`; `False` with `ManagerConflict`, `Observed` (adopt it first), `Invalid`, `Forbidden` (project not linked), `PlaneError`; `Unknown/PlaneUnreachable` |
| `Conflict` | `True` while another field manager owns the plane object |
| `Ready` | the plane holds this generation (not that replicas serve yet) |
| `Stalled` / `Reconciling` | kstatus: a refusal that needs an edit / waiting on the plane |

While the plane is unreachable the operator retries every 30s, and after every reconnect it
reapplies every resource; unchanged ones are no-ops on the plane.

## Delete

Deleting the resource deletes the plane workload first (finalizer `plane.wingsmith.dev/sync`). If
someone has since taken the plane object over (`wings apply --force-conflicts`, console Take over),
it is released and left in place.

**Stuck Terminating while the plane is unreachable.** The resource shows
`Synced=Unknown/PlaneUnreachable` and waits. If the plane will not come back, or you want the plane
object kept, remove the finalizer:

```sh
kubectl patch workload chat -n team-a --type=json \
  -p='[{"op":"remove","path":"/metadata/finalizers/0"}]'
```

The plane workload then stays. It carries `plane.wingsmith.dev/origin-cluster`, `origin-namespace`
and `origin-uid` in its last-applied record (`plane.wingsmith.dev/last-applied`), so leftovers from a
cluster can be found and deleted with `wings` or the API.

## Argo CD

Point an Application at a directory of Workload manifests; nothing Argo-specific is needed in the
files. Argo CD reads `Ready`/`Stalled`/`Reconciling` through its kstatus-compatible health
fallback, and since the operator writes only status and finalizers there is no drift.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: team-a-workloads
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/example/inference
    path: workloads/team-a
    targetRevision: main
  destination:
    server: https://kubernetes.default.svc
    namespace: team-a
  syncPolicy:
    automated: {prune: true, selfHeal: true}
```

A custom health check reading `Synced` and `Conflict` ships later (P1.17). Flux Kustomizations
with `wait: true` use the same conditions.
