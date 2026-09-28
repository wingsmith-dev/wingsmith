# 0010 — Workload is the global umbrella; Deployment stays the per-cluster unit

**Status:** accepted · 2026-08-24, amended by ADR 0014

**Amends:** the founding object model, which made Workload a *supertype* of Deployment.

## Context

Deployment is recipe × cluster: the unit the operator reconciles, one row per cluster. Workload
existed above it as a supertype, joined 1:1 — Deployment was "Workload, specialised for serving" —
which meant the two objects had the same cardinality and Workload carried no fact Deployment did not.

The result is that **nothing in the system expresses multi-cluster intent.** Running one model in two
regions is two independent Deployments that share nothing but a recipe id, plus an Endpoint whose
target weights someone edits by hand to match. There is no object to scale, pause or drain; there is
no place to say "eight replicas total, anywhere in Europe"; and there is nothing for placement to
expand, because placement has already been handed the answer — the cluster — before it is asked the
question. The founding design says cluster selection is global, and the object model had no global
object to hang that selection on.

The 1:1 supertype also made the accommodation it was bought for largely theoretical. The point of one
Workload root over `service | task | gang` is that preemption and quota stay one code path across
serving and batch. A supertype that is always in lockstep with its subtype gives that code path
nowhere to stand: every consumer reached through to the Deployment, and the discriminator only ever
answered a question the caller already knew.

## Decision

**Workload becomes the parent of Deployment. IS-A becomes HAS-MANY.**

    Endpoint    the stable name clients call            global, declared
    Workload    what you asked to run                   global, declared
      -> Deployment   that workload on ONE cluster      per-cluster, declared
          -> Replica  one running copy                  per-cluster, observed

`Deployment.workload_id` survives and its unique constraint is dropped. That dropped constraint is
the whole of the change at the schema level; everything else is fields moving to the level whose
question they answer.

**No existing noun changes meaning.** Deployment is still recipe × cluster, still what
`deployment.desired` carries, still what the operator reconciles. A layer was added above the layer
that already existed, which is why the wire barely moves.

**Facts sort by whether they are global or per-cluster.** Recipe, adapters, overrides, labels, kind,
priority, preemptibility, gang size, deadline, the topology requirement, and the *total* scale are
global and live on Workload. Cluster, admission phase and reason, the queued/admitted/placed
timestamps, and this cluster's *share* of the scale are per-cluster and live on Deployment. The
topology pair moves up because its own justification — "the deployment is the only object that knows
whether this is served or ganged" — dissolves the moment `kind` lives on Workload.

**Scale is declared as a total; placement derives the shares.** Workload carries total
min/max/desired plus optional per-cluster floors and a cap on fan-out. The unit is set by kind:
replicas for `service`, ranks for `gang`, completions for `task`. The default strategy is **pack**,
not spread, so today's single-cluster deployments migrate as a no-op.

**Splittability is a property of kind and placement enforces it.** `service` and `task` split;
`gang` does not — its ranks must be co-scheduled, so a gang workload has exactly one Deployment,
always. The schema permits N and the kind constrains it to one, as a hard rejection rather than a
ranking preference: a split gang is broken, not degraded.

**Placement constraints reuse Policy** rather than growing a parallel set of fields — its subjects
gain `workload`. Explicit cluster pinning survives as the deliberate scheduler escape it already was
and moves up with the rest of the intent.

**Quota and admission stay per-cluster, and therefore on Deployment.** Quota is keyed
(project, cluster, device class) and keeps working unchanged.

**Scaling is an operation:** `workload.scale`, `workload.pause`, `workload.drain`. A scale can sit
pending on quota forever, which is exactly the shape the system reserves for operations rather than
status fields — an operation is imperative, long-running and can fail, and modelling it as a status
field hides all three. `deployment.roll` and `deployment.benchmark` keep targeting one Deployment.

## Alternatives rejected

**Deployment as the umbrella — let a Deployment span clusters.** The cheapest edit on paper: keep one
object, make `cluster_id` a list. It is the merged-object mistake the founding design already
rejected, in a new place, and it fails the same way — one globally unique row reconciled by N
operators simultaneously is two writers on one record. It also breaks the wire, which is the part
that actually costs: `deployment.desired` is what every released operator obeys, and a Deployment
that is no longer "this cluster's copy" means every frame needs a projection step and every operator
needs to learn that most of the object is not about it. Per-cluster admission and quota have nowhere
to live at all; they would become maps keyed by cluster on a global row, which is a table someone has
denormalised by hand.

**A separately-named per-cluster object — `ClusterDeployment`, `Placement`, `Assignment`.** Promote
Workload and *rename* the per-cluster thing, so "Deployment" could become the global noun users say.
Rejected because it renames the one object every part of this system already agrees on: the wire
frames, the released operators, the CLI, the console, the fixture corpus, and ADRs 0006 and 0009,
all of which say "deployment" and mean recipe × cluster. The gain is a friendlier word for the thing
users create; the cost is a rename that touches every layer at once and, worse, a period in which
"deployment" means one thing in Go and another in Python. Adding a noun above is strictly cheaper
than renaming a noun in the middle, and `workload` was already the word for the global thing.

**DeploymentPod / DeploymentGroup — a thin grouping object over N Deployments.** A set with a name
and a member list, no spec of its own; each Deployment keeps its own recipe and scale, and the group
is only for listing them together. Rejected because it is the console feature and not the model
change. The intent still lives in N places, so scaling is still N edits that can disagree, drift
between members is representable and therefore inevitable, and placement gains nothing to expand —
the group has no total to divide. It also defeats the reason Workload exists: a grouping object is
not somewhere preemption and quota can share a code path, because it declares nothing they can read.
The word "pod" is separately unusable in a system that talks to Kubernetes.

**Leave it alone and let Endpoint carry multi-cluster intent.** Endpoint already resolves to N
deployments with weights, so the shape looks available. Rejected because Endpoint is the *routing*
contract and only `service` workloads have one — a task or a gang has no Endpoint and would be left
with no way to express the same thing. Overloading it would also put scale and placement intent on
the one object the model keeps deliberately stable and public.

## Consequences

**Placement acquires a real job.** It expands a Workload into Deployments — candidate clusters,
gang short-circuit, quota headroom, floors, pack/spread fill, fan-out cap, shortfall on partial
placement, and in-place stability so that re-expanding a workload does not shuffle shares that were
already satisfied. Where it previously validated a cluster somebody had already named, it now
chooses.

**Deployment's module becomes read-mostly.** You do not create a Deployment any more; placement does.
It keeps its read routes and its per-cluster operations, and
[`libs/plane/app/modules/workloads/`](../../libs/plane/app/modules/workloads/) becomes the module
users create, list, scale and delete against.

**The wire keeps its names and shapes.** `deployment.desired`, `deployment.observed` and
`deployment.deleted` are untouched, and no fixture changes. The one clarification is that the replica
count on the demand frame is **this cluster's share** and never the workload total — annotated in
[`MESSAGES.md`](../../libs/packages/contract/MESSAGES.md), with the Go operator checked to confirm it
neither sums nor re-derives it. Released operators keep working and the plane keeps accepting their
frames unchanged.

**The min/max range stops being a claim nothing enforces.** Placement sets a Deployment's three
numbers equal, so the bound the console has been drawing is either a real total on the Workload or
absent — it is no longer a range implying an autoscaler that does not exist.

**One-cluster stays one call.** A workload with the default pack strategy and no floors lands
entirely on one cluster and produces exactly one Deployment, which is what existed before. The
migration is a no-op for every existing row, and spreading is something a person opts into.

> **Amended 2026-09-02.** This ADR moved `recipe_id` up onto the Workload along with adapters,
> overrides and the topology pair. ADR 0014 then deleted Recipe, so what sits there is the
> configuration itself — engine, image, args, parallelism, context, cache and device shape —
> identified by `serving_digest`, and the configuration reaches a cluster inlined on
> `deployment.desired.serving` rather than on a frame of its own. "Deployment is recipe × cluster"
> reads "Deployment is one workload's serving configuration on one cluster". The sort this ADR
> performs is unchanged and is what made the removal easy: those facts were already global, and
> once they were on the Workload the separate row was carrying nothing but a name.
