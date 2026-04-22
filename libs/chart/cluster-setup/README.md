# cluster-setup

The tier-2 install that turns a cluster the cloud already prepared (GPUs
schedulable, storage classes bound, a TLS edge) into one the hangar plane can
manage. It installs, in one release:

- the `dynamo` namespace and the shared `model-cache` PVC the operator mounts by name;
- **dcgm-exporter** (subchart) — the exporter the operator scrapes for device identity;
- the **Dynamo platform** (subchart) — operator, NATS, and the `DynamoGraphDeployment` CRD;
- the **hangar operator** and its CRDs, RBAC, config, and Deployment;
- optionally, the serving `Certificate` + Traefik `IngressRoute`.

## Install

```sh
# one-time: authenticate Helm to the chart registry
helm registry login ghcr.io

# secrets the chart references but does not create
kubectl -n hangar-system create secret generic hangar-operator \
  --from-literal=enrolment-token=<the plane's HANGAR_OPERATOR_TOKEN>
kubectl -n dynamo create secret generic hf-token-secret \
  --from-literal=HF_TOKEN=<a Hugging Face token>

helm upgrade --install hangar-cluster \
  oci://ghcr.io/hangar-dev/charts/cluster-setup --version <v> \
  --namespace hangar-system --create-namespace \
  -f values.yaml -f values.local.yaml
```

`values.yaml` is the per-cluster identity block the register-cluster screen
renders (cluster id, hub URL, operator image). `values.local.yaml` is where a
cluster's own infra facts live — pool label scheme, the RWX storage class, the
serving host — kept local and uncommitted. To pin the platform singletons off
the GPU pool, the 1.4.x subcharts take:

```yaml
dcgm-exporter:
  nodeSelector: { <pool-label>: gpu }
dynamo-platform:
  dynamo-operator:
    controllerManager:
      affinity: { ... }        # the operator subchart takes affinity/tolerations, not nodeSelector
  nats:
    podTemplate:
      merge: { spec: { nodeSelector: { <pool-label>: system } } }
```

(A tainted GPU pool already excludes untolerated singletons, so this is
belt-and-suspenders.)

`cluster.id` and `cluster.hubUrl` are required; the chart refuses to render
without them. See `values.yaml` for the full surface.

## Versioning

The chart version is the compatibility unit: `appVersion` pins the operator
image and the chart pins the Dynamo CRD, so a `helm upgrade` moves them
together and the operator can never render into a CRD version it does not know.

## Maintaining

The hangar CRDs in `crds/` are a copy of the source of truth in
`libs/operator/deploy/crd/` — Helm never templates or upgrades a `crds/`
directory, so run `just chart-sync-crds` whenever those change. `just
chart-lint` renders the chart offline; `just chart-push` packages and publishes
it. The subcharts (`dcgm-exporter`, `dynamo-platform`) pull anonymously — no NGC
account is needed. The Dynamo operator applies the `DynamoGraphDeployment` CRD
from its own image (`upgradeCRD`, on by default), so there is no separate CRD
chart to vendor.
