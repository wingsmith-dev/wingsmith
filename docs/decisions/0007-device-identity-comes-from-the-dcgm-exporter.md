# ADR 0007: Device identity comes from the DCGM exporter, and DCGM is an assumed dependency

**Status:** accepted · 2026-08-19

## Context

Kubernetes does not know what a GPU's UUID is. A Node object carries GPU Feature Discovery labels
(`nvidia.com/gpu.count`, `.product`, `.memory`) which say how many cards there are and what they
are, and nothing at all that says which cards. A label value cannot hold eight UUIDs and no vendor
publishes them anyway.

The operator runs as a single Deployment with no node-level access. It therefore minted its device
ids itself: `<node-name>-gpu<index>`, with a `hangar.dev/device-uuids` annotation as the intended
real source, to be written by a bootstrap job or a node agent.

In practice nothing wrote that annotation, so every device carried a synthetic id. The defect this
hides is silent and total: the DCGM exporter reports the true hardware UUID (`GPU-xxxxxxxx-…`), the
metrics store keys every device-scoped series on it, and the plane's metrics module selects on it.
Inventory looks complete and correct while every per-device panel returns nothing, because we are
asking about a GPU nobody has ever heard of. An id we invent is only useful for talking to
ourselves, and a device id is precisely the thing we need to talk to someone else about.

Filling the annotation in would have worked. It was rejected on a principle that outranks the fix:
**hangar discovers, and is not configured for.** Requiring a cluster to carry a `hangar.dev/*`
annotation for hangar's benefit means every cluster it is pointed at needs a change to its own
manifests before it can be seen, and the thing most likely to be missing is the thing nobody
remembered was required. That is exactly the failure we are here to fix, made permanent. Reading
vendor-standard labels that are already there is discovery. Asking to be written to is not.

## Decision

**The operator scrapes the in-cluster DCGM exporter for device identity, and DCGM is a hard
dependency of a hangar GPU cluster.**

A GPU cluster that is observed at all almost always already runs `dcgm-exporter`, because the
telemetry it produces is what the observability stack is built on. It is therefore not a new thing
to install, and its series already carry every identity fact we lack: `UUID`, `gpu` (the index),
`modelName`, and `Hostname`.

Identity precedence, per device slot:

1. **DCGM scrape**: the true hardware UUID, and the model in the `dcgm` dialect.
2. **`hangar.dev/device-uuids` annotation**: retained, unadvertised, for a cluster running no
   exporter. An escape hatch, not a mechanism.
3. **`<node>-gpu<index>`**: the synthetic fallback, unchanged.

An unreachable exporter never fails discovery. It logs, and the node's devices fall down the chain.
Inventory the plane cannot see is strictly worse than inventory whose metrics do not join.

**GFD counts, DCGM names.** The two views are reconciled per node and each is authoritative about a
different question. GFD's `gpu.count` is what the kubelet advertises and the scheduler allocates
against, so a device GFD does not count is not a device the plane can place on, however plainly
DCGM can see it. DCGM sees hardware, which is a different question and not the one placement asks.
On disagreement GFD wins the count, both numbers are logged, DCGM entries past the count are
dropped, and slots DCGM did not name fall through to the annotation and the synthetic id. If GFD
says nothing at all, DCGM's count is used, so a cluster without feature discovery still yields
inventory. Neither view is ever discarded silently.

Device class continues to resolve through `libs/packages/contract/devices.json` and its existing
loader. The dictionary already indexes both dialects for exactly this reason: GFD's
`nvidia.com/gpu.product` is the `gfd` alias and DCGM's `modelName` is the `dcgm` alias for the same
card.

## Why this does not put the data plane on the control plane

The data plane never crosses the control plane. This reads a Prometheus exporter, which
looks like telemetry ingestion and is not.

- **What is read is state, not measurement.** A UUID, an index, a model name and a host. These are
  membership facts: bounded by the hardware, changing only when a card is physically moved, and
  exactly the kind of thing the supply stream exists to carry. The sample value on each series
  (utilization, temperature, power, throughput) is read by nothing here and is not in any struct
  this code defines.
- **The read is cluster-local.** One HTTP GET from the operator to a Service in its own cluster,
  once per observation tick regardless of node count, cached across every node in the snapshot. No
  byte of it reaches the plane except the identity in `device.observed`.
- **Telemetry's path is unchanged.** Measurements still go from the cluster's exporter to the
  observability stack directly (PROTOCOL §7, ADR 0003). Nothing is proxied, forwarded or
  re-exported. If this operator is down, the metrics keep flowing.

The distinction to hold onto: we read the exporter to learn *which GPU this is*, and the
observability stack reads it to learn *how that GPU is doing*. Only the first belongs on our wire,
and after this change the two finally agree on the name of the thing.

## Consequences

A GPU cluster with no DCGM exporter is a degraded cluster: it still enrols, still reports inventory,
still takes placement, and its device metrics do not join. That is a documented state, not a
surprise: the operator says so in its logs on every tick.

`HANGAR_DCGM_ENDPOINT` is the one new knob, defaulting to
`dcgm-exporter.platform.svc.cluster.local:9400`. Setting it empty disables the scrape.

No RBAC changed. The exporter is reached by Service DNS over ordinary HTTP; nothing here asks the
Kubernetes API for a Service, an Endpoint or anything else it could not already list.

Device ids are minted from the UUID (ADR 0002), so a node whose devices were previously synthetic
gets new ids the first time the exporter answers. Old device rows prune out on the next observation
and the ids that replace them are the ones the hardware will keep for the rest of its life.
