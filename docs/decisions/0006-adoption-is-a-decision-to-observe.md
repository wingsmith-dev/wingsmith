# ADR 0006: Adopting a running workload is a decision to observe, never to declare

**Status:** accepted · 2026-08-19 · amended by ADR 0009

## Context

The operator is routinely pointed at clusters that are already serving: models running, weights
resident in VRAM and traffic on them, none of it declared by the plane.

The obvious shape is an import: read the running Deployment, synthesise a recipe and a
`demand_deployment` row, and let the reconciler take it from there. That shape restarts every model
it touches, and the failure is not a bug in it: it is what it means.

A declared object is something the cluster is obliged to match. The moment one exists, the operator
has a difference between what we say and what is running, and closing that difference is a rollout.
The difference does not have to be large. A recipe that renders `image: vllm:0.9.1` where the pod
runs the digest it resolved to, a default resource limit we add, an env var in a different order:
any of these is a new pod template, and a new pod template is a fresh process loading seventy
billion parameters off disk. Minutes of degraded serving per model, for a bookkeeping operation the
operator thought was read-only.

## Decision

Discovery and adoption produce **no declared state at all**.

- The operator reports `workload.discovered`: a sighting. Observed, full-object, on the supply
  stream, pruned like any other observation.
- Ingestion writes it to `demand_discovered_workload` and is that table's only writer.
- `POST /v1/discovered/{ref}/track` writes one row in `demand_workload_adoption`: a decision, an
  owner, a note, who decided. A different table because it has a different writer.
- `stream_desired` reads neither table. There is no path from a tracked workload to a
  `deployment.desired`, so a reconciler is never handed anything to converge.

Tracking a workload means it appears in the inventory with a name and a project. It does not mean
the plane manages it. The workload keeps running exactly as its own cluster has it configured.

## Consequences

A tracked workload is visible but not controllable: it cannot be scaled, rolled or upgraded through
the plane, because the plane has declared nothing to change. That is the correct trade for now:
seeing what is running across every cluster is the immediate need, and it is worth having without
risking a single restart to get it.

Promotion (letting the plane manage a workload it adopted) is a separate, explicit act, and it is
built. See the amendment below.

The prune interacts with the decision, so adoptions are keyed by the cluster's stable workload uid
instead of a foreign key. A resync that arrives while a workload is briefly absent takes the
sighting away; the decision survives it, and the workload comes back tracked. Ids are minted by the
cluster from namespace, kind and name (ADR 0002), so they are the same across operator restarts and
across replicas.

`kind` is open text on the wire where every other enum is closed. A cluster may run models under a
CRD we have never heard of, and a closed set would make that workload undecodable: dropped instead
of reported, which is the one outcome an inventory must not have.

## Amendment: promotion, and why it is not an operation

This ADR originally said promotion must be an `operation.desired`, so the cluster could prove the
rendered spec matched what was live before anything was declared. It is a synchronous call instead,
and the reason is worth writing down: the check the operation existed to perform is already answered
by data the cluster publishes continuously.

Every `workload.discovered` carries `spec_fingerprint`, a digest of the pod template: what the
workload is running and, by design, not how much of it, so scaling never looks like redefinition.
`POST /v1/discovered/{ref}/promote` requires the caller to send back the digest they were shown and
refuses if the sighting has moved since. The deployment it creates records that digest in
`adopted_fingerprint`, and every `deployment.desired` for it carries an `adopt` block naming the
live object and the digest it was pinned to.

The gate then lives where it can actually be enforced. `reconcileAdopted` in the operator
([`libs/operator/internal/reconcile/`](../../libs/operator/internal/reconcile/)) reads the live
object, digests it, and on any disagreement sets `AdoptionDrifted` on status with the two digests
and does no cluster work: it does not return an error, because nothing here is retryable; a human
has to look. An object it cannot read is `AdoptionUnverified`, which is equally a refusal: an
adopted object that has gone missing is not one we may recreate, because recreating it is a cold
start of the whole workload.

A sighting can be one observation interval stale, so a workload that changed in the last thirty
seconds can still be promoted. That is safe in the only direction that matters: the next observed
frame updates the digest, the recorded one no longer matches, and the operator refuses to reconcile
and says why. The gate moves from "refuse to promote" to "refuse to reconcile" and fails closed
either way.

What promotion buys is management. The deployment is real, so scale, labels, priority and
`enabled` all work through `/v1/deployments` as they do for anything else, and none of them touch
the fingerprint: scaling an adopted workload is free. Changing the artifact or the overrides bumps
the generation and rolls it, which is correct: that is a change somebody asked for, and paying for
it is the point. The distinction this ADR defends is only ever between a cost you chose and a cost
you incurred by looking.

One thing promotion could not do when this was written, and this was the real limit: there was no
reconciler, so a promoted deployment was declared, streamed and verified, but no Kubernetes object
was written from it. `adopt` is a contract the reconciler was born holding instead of a behaviour
bolted on after it exists.
