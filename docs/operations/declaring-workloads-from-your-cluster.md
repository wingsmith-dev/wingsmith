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
| `Synced` | the plane accepted this generation: `True/Applied`; `False` with `ManagerConflict` (another field manager owns the plane object), `Observed` (adopt it first), `Invalid`, `Forbidden` (project not linked), `PlaneError`; `Unknown/PlaneUnreachable` |
| `Ready` | serving. `Unknown/AwaitingStatus` for now: serving status from the plane arrives in a later release |
| `Stalled` | `True` on a refusal that needs an edit (`ManagerConflict`, `Observed`, `Invalid`, `Forbidden`), or `DeleteFailing` after 10 failed plane deletes |
| `Reconciling` | `True` while waiting on the plane or on serving status |

`kubectl get workloads` shows Synced, Ready and Age. The conditions follow kstatus: Stalled=True is
failed, Reconciling=True is in progress. A resource the operator has not written status to yet has
no conditions, which kstatus reads as Current; wait on `condition=Synced`, not on kstatus
alone right after an apply.

**Schema.** The CRD checks the spec loosely (unknown fields are kept) and the plane validates it
fully, so a typo is reported as `Synced=False/Invalid` after the apply, not refused by the
API server. A generated schema replaces it in a later release.

While the plane is unreachable the operator retries every 30s, and after every reconnect it
reapplies every resource; unchanged ones are no-ops on the plane. The relist goes through a token
bucket (`cluster.declare.qps`, default 10, `burst` 50) and at most `concurrency` (default 4)
reconciles run at once, so a reconnect with thousands of resources does not flood the plane.

## Delete

Deleting the resource deletes the plane workload first (finalizer `plane.wingsmith.dev/sync`).
`spec.deletionPolicy: Orphan` (default `Delete`) leaves the plane workload in place instead. If
someone has since taken the plane object over (`wings apply --force-conflicts`, console Take over),
it is released and left in place. A plane that no longer knows the object (`invalid`) also releases it.

**Stuck Terminating on a plane refusal.** A delete the plane refuses (a conflict, or a plane error)
is retried with exponential backoff (30s doubling, capped at 16 minutes). After 10 failed attempts
the resource shows `Stalled=True/DeleteFailing` with the plane's reason and keeps retrying at the
cap. To let it go and keep the plane object, patch the policy:

```sh
kubectl patch workload chat -n team-a --type=merge -p '{"spec":{"deletionPolicy":"Orphan"}}'
```

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
files, and since the operator writes only status and finalizers there is no drift. Add the health
check from `libs/operator/deploy/argocd/workload-health.lua` to `argocd-cm` (no status is
Progressing, Stalled or Synced=False is Degraded, Ready=True is Healthy, anything else
Progressing):

```yaml
data:
  resource.customizations.health.plane.wingsmith.dev_Workload: |
    # contents of workload-health.lua
```

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

## Flux

A Kustomization with `wait: true` reads kstatus; to wait for acceptance instead of serving, add:

```yaml
spec:
  wait: true
  healthCheckExprs:
    - apiVersion: plane.wingsmith.dev/v1alpha1
      kind: Workload
      current: status.conditions.filter(e, e.type == 'Synced').all(e, e.status == 'True')
      failed: status.conditions.filter(e, e.type == 'Stalled').all(e, e.status == 'True')
```
