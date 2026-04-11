# Message types

The envelope, correlation rules, and resume semantics are in [`PROTOCOL.md`](PROTOCOL.md). This
document is the type registry: what exists, which way it flows, and what its payload holds.

Naming is `<noun>.<verb>`. The noun is an object from
[`docs/plans/01-object-model-and-architecture.md`](../../../docs/plans/01-object-model-and-architecture.md);
the verb says what kind of frame it is.

| Verb | Direction | Shape |
|---|---|---|
| `.desired` | hub → operator | declared state for one object. Full object, not a patch. |
| `.observed` | operator → hub | observed state for one object. Full object, not a patch. |
| `.event` | operator → hub | something happened, at a point in time. Never inferred from state. |
| `.request` / `.response` | either | correlated pair, per §3 |

**The cluster mints the ids of the things it observes.** `node.observed` carries the `nod_` id, not
a name the plane will later assign an id to; `device.observed` names its node by that same id.
Identity originates where observation does, because an observed object needs a handle from the
moment it is discovered, and the plane cannot supply one for a thing it has not yet heard of. The
plane stores the id as given and never re-mints it — see ADR 0002.

This makes id shape contractual. An observed id is its prefix followed by a Crockford base32 ULID
body, and a frame carrying a malformed one is not something a replay can fix (see §"Poison frames"
in [`PROTOCOL.md`](PROTOCOL.md)).

**Full objects, never patches.** A patch stream that loses a frame is unrecoverable; a full-object
stream that loses a frame is stale until the next one, which resume then fixes. The cost is
bandwidth we do not care about at this scale.

---

## Session

| Type | Direction | Payload |
|---|---|---|
| `hello` | op → hub | `cluster_id`, `agent_version`, `session_id`, `resume_from`, `lossy`. **Unsequenced.** |
| `welcome` | hub → op | `session_id`, `ack_seq`, `full_resync`. **Unsequenced.** `reply_to` is the `hello`'s id. |
| `ping` / `pong` | either | empty. **Unsequenced.** |
| `error` | either | `code`, `message`, `retryable` |
| `ack` | either | empty. **Unsequenced.** Carries acknowledgement in the envelope's `ack` field, for when there is nothing else to say. |
| `stream.end` | either | `count`, `complete`. **Sequenced**, unlike the other session frames — a prune decision depends on it, so it must not be able to arrive out of order or be lost silently. |

## Supply — operator → hub

| Type | Payload |
|---|---|
| `node.observed` | identity, membership, conditions, cordon state, kernel / driver / container-runtime / agent versions |
| `node.event` | joined, left, cordoned, uncordoned, degraded |
| `device.observed` | per-accelerator: uuid, model, memory capacity, health, ECC and throttle state, MIG profile, current allocation |
| `device.event` | degraded, recovered, reset, fell off the bus |
| `topology.observed` | which devices share an NVLink domain, PCIe root, NUMA node, and which nodes the domain spans |
| `capacity.observed` | derived rollup per pool: free, allocated, unhealthy, by device class |
| `workload.discovered` | a workload already running in the cluster that the plane never declared |

Device metrics — utilization, temperature, throughput, memory in use — are **not** here. Device
memory appears only as `memory_total_mb`: capacity is state, occupancy is measurement. See
PROTOCOL §7.

**A discovered workload is a sighting, not a deployment.** `workload.discovered` reports what was
already running when the operator arrived — the pods holding GPUs that the plane did not put
there. It is supply-side because it describes what a cluster is doing with its own hardware, and
it is a distinct type from `deployment.observed` for one reason: `deployment.observed` is the
cluster reporting on something the plane declared, and there is nothing declared here. Adopting a
sighting attaches a name and a project to it; it never produces a `deployment.desired`, because
declaring what is already running is how a control plane restarts it.

**A sighting carries `spec`: how the workload was told to serve, uninterpreted.** The image, the
container's argv, the resources it asked for and its node selector, verbatim as its own cluster
records them. This is what makes a sighting adoptable rather than merely visible — engine and
version live in the image tag, context length and parallelism in the arguments, and without them
adopting a running model means retyping by hand what the cluster already knows. The operator reads
it off the container holding the accelerators and interprets none of it; deciding that
`--tensor-parallel-size` means tensor parallelism is the plane's job, so that a better reading is a
plane deploy rather than a roll across every cluster in the fleet.

**`spec.env_names` is names without values, and that is a guarantee.** A serving pod is routinely
handed a HuggingFace token or a registry credential through its environment, usually by `envFrom` a
secret. Values would be copied into the plane's database and into every backup of it, to no
purpose: the names already tell a reader what the workload expects to find. A secret or config-map
reference appears as `secretRef:<name>`, naming the source without reading it.

**`deployment.desired` carries `authority`, and it is the only thing that decides whether the
operator may write.** `observed` means record and verify and touch nothing; `managed` means render
and converge. An absent value reads as `observed`, so a frame from a plane that predates the field
never acquires management by omission. The two are a state a person moves a deployment between, in
both directions, and there is deliberately no third value — a deployment with some fields enforced
and others drifting cannot be reasoned about at the moment somebody needs to.

**`deployment.desired` carries `adopt` when it was promoted from a sighting.** It names an object
that already exists and is already serving, so a managed deployment takes ownership in place rather
than applying a rendered spec over it. `spec_fingerprint` digests the pod template — what is
running, deliberately not how much of it, so scaling never reads as redefinition. An operator that
finds the live object digesting to anything else must refuse and report it, never converge; closing
that gap means restarting a model to match a spec nobody asked to change. `adopt` is provenance and
a drift check and is **not** itself a prohibition: whether the plane may act is `authority`'s
question, because where an object came from is a historical fact and whether we run it is a decision
(ADR 0009).

**A topology domain names nodes, plural.** `topology.observed` carries `node_ids` and
`spans_nodes`, not a single `node_id`. An NVLink domain over an NVSwitch fabric or a GB200 rack
crosses machines, which is precisely the shape topology exists to describe; a domain confined to
one machine simply carries one id, and a domain whose nodes are not yet known carries none.
`spans_nodes` is true when more than one node is named — the plane reads it rather than recomputing
it, so the cluster stays the authority on its own fabric.

**Cordon and uncordon are both events.** A cordon is an operator decision and so is its reversal;
an event log that records only one of the two forces the reader to diff `node.observed` to learn
when a node came back, which is the inference `.event` frames exist to avoid.

## Demand — hub → operator

| Type | Payload |
|---|---|
| `deployment.desired` | recipe reference, scale bounds, priority, placement constraints |
| `deployment.deleted` | id |
| `recipe.desired` | how to serve an artifact: engine, image, declared args, parallelism, cache settings |
| `artifact.desired` | what weights should be resident, and where they come from |
| `component.desired` | what is installed and at what version |
| `cache.desired` | the artifact warm-set for this cluster |

**A recipe travels as its own declared object, not inlined on the deployments that name it.**
`deployment.desired` references it by `recipe_id` and `recipe_revision`, the way it already
references artifacts by id. This is the split KServe draws between `ServingRuntime` and
`InferenceService`, for the same reason: recipes are shared. Several deployments of one model on
one cluster differ in scale and placement rather than in engine configuration, so inlining would
ship the same body repeatedly and turn one recipe edit into a rewrite of every frame naming it.

The cost is an ordering dependency, and the stream pays it the same way it already does for
artifacts: recipes precede the deployments that reference them. A receiver that sees a deployment
whose recipe has not landed — possible after a resume, not after a resync — waits rather than
renders, because a render against a missing recipe is a guess.

The device shape is deliberately absent from `recipe.desired`. It reaches the cluster on the
deployment's `placement`, derived from that same recipe. Two copies of one fact on one wire is two
things that can disagree, and the placement copy is the one placement already reads.

## Demand — operator → hub

| Type | Payload |
|---|---|
| `deployment.observed` | phase, `reason`, `message`, replica counts, last error |
| `replica.observed` | which devices, readiness, engine version, restart count, observed generation |
| `replica.event` | started, ready, failed, OOM, preempted |
| `allocation.observed` | devices currently committed, and to what |
| `cache.observed` | artifact residency, per node, and eviction pressure |
| `component.observed` | rollout status per component |

**`deployment.observed` carries `reason` and `message`, and the operator sends one on every
outcome — including the ones that are not errors.** A deployment spends most of its life before it
serves waiting for orderings to be satisfied: the recipe has not arrived, the weights are still
downloading, no node advertises enough accelerators. Each of those is a correct, expected state and
none of them is a failure, so none of them belongs in `last_error`. Without them the plane sees a
deployment at `pending` for twenty minutes and can say nothing about which of six things it is
waiting on, while the operator knows exactly and says so in a log line in another cluster.

`reason` is a closed set owned by the operator, because a set the plane also writes into is a set
nobody can enumerate. `message` is free text for a person and is never parsed. Both are optional:
absent means the cluster is not distinguishing a sub-state, not that it is healthy.

| `reason` | Phase | Means |
|---|---|---|
| `RecipeNotDeclared` | `pending` | The recipe this deployment names has not reached the cluster |
| `ArtifactNotDeclared` | `pending` | The artifact that recipe serves has not reached the cluster |
| `WeightsNotResident` | `pending` | The weights are not yet cached on this cluster |
| `WarmSetIncomplete` | `pending` | The cache is fetching, and this artifact is not done |
| `NoSchedulableCapacity` | `pending` | No node advertises enough accelerators of the class |
| `NoClusterWork` | `pending` | Held as declared state; `authority` is `observed` |
| `Applied` | `placed` | Rendered and applied; replicas not yet reported ready |
| `PayloadUndecodable` | `failed` | The desired frame did not decode |
| `RenderFailed` | `failed` | The frame decoded and describes nothing renderable |

A `reason` the plane does not recognise is displayed, never rejected — the operator rolls
independently and a new sub-state must not need a plane deploy to be visible. The phase column is
what each reason means today and not a constraint the receiver enforces; the two travel together
and the phase is the one anything computes on.

**A waiting frame repeats only when it changes.** The reconciler re-examines a waiting deployment
every fifteen seconds, and sending each pass would spend the stream on a fact that has not moved.
The operator holds the last `(phase, reason, message)` it sent per deployment and sends only on
difference, which is the same rule the supply loop already applies to nodes.

**`replica.observed` carries the replica's `name`.** The cluster-local name — the pod name under
Kubernetes — alongside the `rep_` identifier. They are not redundant: the identifier is what other
frames cross-reference, the name is what an operator types into `kubectl` when a replica is wedged,
and neither is derivable from the other.

**`replica.observed` carries `observed_generation`.** A replica's phase says whether it is up; the
generation says which version of the deployment it is up *at*. A rollout is exactly the interval in
which those two disagree, so a reader without the generation cannot distinguish "four ready" mid-roll
from "four ready" when it has finished — and the plane would have to infer the difference from
engine version, which is not the same thing and moves for other reasons.

**`allocation.observed` carries an `allocation_id`.** Every observed object is named by the cluster
that discovered it (ADR 0002), and an allocation is no exception. Keying it by `replica_id` instead
would work only while allocations and replicas stay one-to-one, and would leave the plane minting
identifiers for rows it does not own the moment they do not.

**`cache.observed` reports residency per node.** `nodes` carries a state and a byte count for each
node holding any part of the artifact; the top-level `state`, `resident` and `bytes_resident` remain
the cluster-wide rollup, because a shared tier has residency with no node to attribute it to. A node
mid-fetch and a node already serving are different facts to a cache-aware router, and a flat list of
node ids cannot tell them apart.

## Operations

| Type | Direction | Payload |
|---|---|---|
| `operation.desired` | hub → op | typed: drain, cordon, reboot, device reset, cache warm, roll, upgrade, benchmark. Carries target, reason, requested_by, deadline |
| `operation.observed` | op → hub | phase, progress, result, failure |

Operations are the third kind of thing — imperative, long-running, able to fail. They are not
status fields on the objects they act upon, and modelling them as such is the mistake this
registry exists to prevent.

**Operations are outside the resync contract.** A full-resync snapshot is the authority for pruning
declared state, and an operation absent from one has not been withdrawn — it is not the kind of
thing a snapshot describes. Pruning against one would cancel work already half-done, so an
operation is accepted once, run to a conclusion, and reported. The consequence is that delivery is
at-least-once with no replay window: a receiver drops a redelivery of an operation it is already
running rather than starting a second pass over the same node.

**`cordon` covers both directions.** The parameter `cordon` is `"true"` or `"false"` and defaults
to `"true"`; there is no separate `uncordon` kind, because one wire kind and a boolean say the same
thing as two kinds without doubling the vocabulary. `drain` cordons first as its own first step, so
the two compose rather than overlapping.

**`drain` evicts rather than deletes, and only what holds accelerators.** Eviction is the request
the API server can refuse, so a pod covered by a disruption budget already at its floor stays put
and the drain reports what it could not move. Deleting would succeed every time and cause the
outage the operation exists to avoid. Pods that hold no accelerators are left alone: the agents,
exporters and CSI drivers on a node are what make it reportable, and taking them out would blind
the plane to the drain it just requested.

**`benchmark` measures a deployment and reports its result somewhere else.** The operation is how
the run is carried out — it has the phases, the deadline, the cancellation and the replay every
other operation has — but its outcome does not fit `operation.observed.result`, which is a flat
`map[string]string`. A load generator emits nested JSON, so the numbers travel on
`benchmark.observed` and the operation reports only that the errand finished. The parameters name
the run (`benchmark_id`), the harness (`harness`, `image`), the model to ask for (`model`) and
whatever knobs that harness accepts; the receiver runs it against the cluster's own inference
endpoint and never dials out.

## Benchmarks

| Type | Direction | Payload |
|---|---|---|
| `benchmark.observed` | op → hub | benchmark_id, harness, harness_version, phase, summary, artifacts_uri, error |

**`summary` is the harness's own output shape and is not normalised.** What AIPerf calls a
percentile and what vLLM's `benchmark_serving.py` calls one are not the same field, and a schema
here asserting they are would be this protocol designing a normalisation before either format has
been read against a real deployment. Two runs are comparable when `harness` and `harness_version`
agree; that is the whole guarantee, and it is deliberately not more.

**Per-request records do not travel this stream.** A run's raw output — one row per request, full
latency distributions — is telemetry: high volume, append-only, never joined. It goes from the
cluster to object storage directly and `artifacts_uri` is the only part of it that comes here.

## Resync

| Type | Direction | Payload |
|---|---|---|
| `resync.begin` | either | `scope` — `supply | demand | all` |

`reply_to` is the sole correlation between a `resync.begin` and its terminating `stream.end`. A
receiver handling concurrent resyncs of different scopes matches them on that and nothing else,
which is why `stream.end` carries no `scope` of its own.

A resync opens with `resync.begin` and closes with the ordinary `stream.end`; there is no
`resync.end`. Two terminators with identical payloads would be two ways to say one thing, and a
receiver that has to handle both will eventually handle only one.

Everything between is ordinary `.desired` / `.observed` frames sharing one `reply_to`. There is no
special resync encoding — a snapshot is just the whole set, sent at once. **`stream.end` carries
`complete`**, and a snapshot that does not end with `complete: true` must not be pruned against:
the receiver keeps what it has rather than deleting against a partial view.

---

## Status

**Draft.** The verbs, directions, and the full-object rule are settled. Payload field lists are
indicative and become binding as each type gets a fixture in [`fixtures/`](fixtures/). A type with
no fixture is not part of the contract, so nothing here is real until it has one.
