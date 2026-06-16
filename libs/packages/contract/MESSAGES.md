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
| `hello` | op → hub | `cluster_id`, `operator_version`, `session_id`, `resume_from`, `lossy`. **Unsequenced.** |
| `welcome` | hub → op | `session_id`, `ack_seq`, `full_resync`. **Unsequenced.** `reply_to` is the `hello`'s id. |
| `ping` / `pong` | either | empty. **Unsequenced.** |
| `error` | either | `code`, `message`, `retryable` |
| `ack` | either | empty. **Unsequenced.** Carries acknowledgement in the envelope's `ack` field, for when there is nothing else to say. |
| `stream.end` | either | `count`, `complete`. **Sequenced**, unlike the other session frames — a prune decision depends on it, so it must not be able to arrive out of order or be lost silently. |

## Supply — operator → hub

| Type | Payload |
|---|---|
| `cluster.observed` | what the cluster turned out to be: distribution, cloud, region, its node and device counts, and whether the operator is read-only |
| `node.observed` | identity, membership, conditions, cordon state, machine shape (instance type, cloud, zone, region, cores, memory, architecture, os image), taints, kernel / driver / container-runtime / operator versions |
| `node.event` | joined, left, cordoned, uncordoned, degraded |
| `device.observed` | per-accelerator: uuid, model, memory capacity, health, ECC and throttle state, MIG profile, current allocation |
| `device.event` | degraded, recovered, reset, fell off the bus |
| `topology.observed` | which devices share an NVLink domain, PCIe root, NUMA node, and which nodes the domain spans |
| `capacity.observed` | derived rollup per pool: free, allocated, unhealthy, by device class |
| `workload.discovered` | a workload already running in the cluster that the plane never declared |

Device metrics — utilization, temperature, throughput, memory in use — are **not** here. Device
memory appears only as `memory_total_mb`: capacity is state, occupancy is measurement. See
PROTOCOL §7.

**`read_only` is the only field on the wire about the operator rather than the cluster.**
A read-only operator runs without its reconcile loop: it reports everything and applies nothing the
plane declares. It is carried because the plane cannot otherwise tell an operator that declines to
act from one that has not acted yet — placement picks the cluster, the hub streams the desired
state, the session stays healthy, and the workload sits at zero replicas with no error to explain
it. Like `distribution` it is inert: stored and displayed, never branched on. It defaults to false,
so an operator built before this field reads as a normal one, which is what it is.

**`multinode` is the one observed field on this frame that placement reads.** It says a
leader-worker backend — Grove or LeaderWorkerSet — is installed, so Dynamo can render a replica
whose devices do not fit on one machine. It is discovered from the API groups the cluster serves,
not declared in the operator's configuration: the CRDs are either present or they are not, and a
flag asserting otherwise is a second copy of the same truth that can disagree with it. When it
does disagree the cluster wins silently — the render produces one pod holding a fraction of the
devices the engine expects, which hangs in collective initialisation rather than failing. Unlike
`read_only` and `distribution` this one is therefore not inert: a spanning replica must not be
placed where it cannot run. It defaults false, which reads an operator too old to send it as one
that could not have detected a backend either.

**`distribution` is the one orchestrator name on the wire.** Every other field is deliberately
neutral, and this one is not: `eks`, `rke2`, `gke` name a Kubernetes flavour. It is carried because
an operator needs to know what they are running and there is no neutral way to say it, and it is
bounded by being inert — the plane stores it and displays it and nothing branches on it. A plane
that reads `distribution` to decide what to send has broken rule 2.

**The machine shape is stated in neutral units.** `cpu_cores` is a count and `memory_mib` is
mebibytes, resolved by the operator, rather than the orchestrator's own quantity strings. The plane
must never have to parse `"128974848Ki"` to learn how big a node is.

**Absent is not empty.** A field the operator omits leaves the plane's value alone; it is not
written as null. This is what lets an operator predating a field roll alongside one that carries it
without the older one blanking what the newer wrote. `taints` is therefore genuinely optional
rather than defaulting to `[]` — an empty taint list is a fact worth recording, and it has to be
distinguishable from a frame that never mentioned taints.

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
| `deployment.desired` | name, the `serving` block — engine, worker and frontend images, declared args, parallelism, cache settings — this cluster's scale bounds, priority, placement constraints |
| `deployment.deleted` | id |
| `artifact.desired` | what weights should be resident, and where they come from |
| `component.desired` | what is installed and at what version |
| `cache.desired` | the artifact warm-set for this cluster |

**`deployment.desired` carries the deployment's `name`, and the operator renders under it.**
The id is what everything is keyed by and the name is what everything is called: the objects an
operator creates inherit their names from the workload's, so an id there reaches all the way down
to `dep-01m0sktt1s3vszhp5rkdp925dh-vllmdecodeworker-fadcdb6d-7zgd5s` on a pod. A name is unique
within a cluster, which is what makes it usable as an object name; a receiver that finds it empty
or unrenderable falls back to the id.

**The scale bounds on `deployment.desired` are this cluster's share, never a workload total.**
A workload declares totals across every cluster it may use; the plane divides them and each operator
receives only its own portion. An operator sees one cluster and cannot tell a whole workload from a
fragment of one, which is the point — nothing in a cluster ever needs to know it is one of several.
A receiver renders exactly the number it is sent and must never sum, scale, or re-derive it against
anything else.

`min_replicas` is what is rendered; `max_replicas` is a ceiling for an autoscaler that does not
exist yet, and nothing reads it.

`deployment.observed.desired_replicas` reports what this cluster rendered — the operator's own
`min_replicas`, echoed back so the plane can see that render and declaration agree. The plane sums
it across a workload's deployments; the operator never does.

**The serving configuration travels inlined, on `deployment.desired.serving`.**
It used to be a declared object of its own, `recipe.desired`, referenced by `recipe_id` and
`recipe_revision` — the split KServe draws between `ServingRuntime` and `InferenceService`, on the
argument that recipes are shared and inlining would ship the same body repeatedly. That argument was
wrong about this stream. The demand set is filtered and serialised per cluster, so a configuration
two deployments shared was already written out once per cluster and saved nothing across them;
only two deployments of one configuration on one cluster ever saved a body, and that is the canary
case and nothing else.

What the separate frame did buy was an ordering hazard. A deployment could arrive naming a
configuration that had not landed and wait for it, and after a resume that replayed the deployment
but not the recipe it waited for ever. Inlining removes the frame, the ordering rule, and the wait.

The stream still pays an ordering dependency for artifacts, and now only for artifacts:
`artifact.desired` precedes the deployments that reference it, and a receiver that sees a deployment
whose artifact has not landed — possible after a resume, not after a resync — waits rather than
renders, because a render against missing weights is a guess.

`serving.digest` names the configuration by its content, and is the same `serving_digest` the plane
holds on the workload. It is not a revision counter and nothing orders by it: it is there so two
deployments that would produce the same running process can be recognised as such, on either side of
the wire, without comparing every field.

**The model id an API client sends is declared, not derived from a name.**
`deployment.desired.served_model_name` is what the engine is told to answer to, resolved by the
plane and never derived in the cluster. It is the artifact's slug — `moonshotai/kimi-k3` — stored on
the artifact when the weights are registered rather than reconstructed at read time. The workload's
name is the floor below it, for an artifact registered before slugs existed or one whose locations
say nothing resolvable. Something always answers, so the field is never empty on the wire and a
receiver never has to invent a fallback.

The floor is a bad answer and is meant to look like one — it is a workload's name where a model id
belongs — because the alternative is rendering `--served-model-name ` with nothing after it and a
frontend that registers no model at all.

Two levels have been removed from below this, and both for the same reason: a name that is not a
fact about the weights renames the model for every caller whenever anything else changes. The
recipe's name went first — it put the fit on the public API surface (`kimi-k3-mxfp4-tp8-1024k`), so
re-fitting the same weights at a different tensor degree renamed the model every client codes
against. The workload's own `served_model_name` went with it: it existed because the plane could not
produce an organisation-qualified slug and somebody had to type `openai/gpt-oss-20b` by hand, which
is a workaround wearing the clothes of a feature. The slug is a fact about the weights, it survives
a re-fit, and it is now stored rather than guessed.

`engine_args["served-model-name"]` still beats it, because `engine_args` is laid over the derived
flags and an escape hatch that loses to a default is not one. The plane leaves that hatch out of
what it resolves, so the same value never travels twice.

**A runtime image is not an engine version, and the block names the first.**
`serving.engine_runtime_version` is a tag of the Dynamo runtime image — `1.4.1` — and `serving.engine` is
the module inside it the worker launches, `python3 -m dynamo.<engine>`. The engine's own version is
neither field and is not on this wire at all: which vLLM NVIDIA compiled into `vllm-runtime:1.4.1`
is a property of that image, unrecorded here and unobserved by the cluster. The two count in
different sequences — vLLM is on `0.28.0` while the runtime carrying it is on `1.4.1` — so a block
that spelled the runtime tag `engine_version` invited exactly one mistake, pinning the engine's
number as the image's, and `vllm-runtime:0.28.0` is an address that has never existed.

**The serving block carries two images, because a frontend and a worker are two binaries.**
`serving.engine_runtime_image` is what loads the weights. `serving.frontend_image` is what
this model's own Dynamo frontend runs, and it is a field of its own rather than a reuse of the
first because the two are versioned separately and the difference is not theoretical. Kimi K3
arrived on a worker running the stock `vllm-runtime:1.4.0-patched.1` and served: 191.74 GiB
across eight devices, loaded and healthy. The cluster's frontend, one version later, refused the
model's tokenizer — `Unsupported tiktoken model_type 'kimi_k3'` — and answered `/v1/models` with an
empty list. The tokenizer table is compiled into the frontend binary, so the fix was a frontend
build and the worker correctly stayed where it was. One image field cannot say that, and a plane
that only had one would have had to move the image every model on that cluster is served through
in order to reach one of them.

`frontend_image` is empty in the ordinary case, and **empty means the cluster decides**. What a
cluster runs its frontends on is a local fact: the operator holds it and the plane does not, so
the plane declares an override or declares nothing. It never substitutes `engine_runtime_image` for an
absent `frontend_image`, because on a cluster whose frontends are not published from the engine's
repository that is an `ImagePullBackOff` chosen a continent away, and because a value the plane
invented is indistinguishable on arrival from one somebody pinned.

The cluster resolves it most specific first: this deployment's `serving.frontend_image`, then
whatever that cluster is configured to serve its frontends through, then — last, and only when
neither has been said — the same block's `engine_runtime_image`. That last step is not the plane's rule leaking back in. It is
right often enough to be the honest default, since the Dynamo runtime image carries the frontend
and the engines together, and it is reached only after the two places that could have said
something better both declined. An empty string is not an override at any level and is treated as
absent, per §2.

The device shape is deliberately absent from `serving`, and stays absent now that the block travels
on the deployment itself. It reaches the cluster on the deployment's `placement`, derived from that
same configuration. Two copies of one fact on one wire is two things that can disagree, and the
placement copy is the one placement already reads.

**How many machines a replica lands on is derived by the cluster, not declared by the plane.**
`placement.devices_per_replica` says how large the replica is and `serving.parallelism.tensor` says
how much of it has to share a node; how those become pods depends on how big this cluster's
machines are, which is a local fact. So a replica of four devices is one pod on a four-device
cluster and two pods on a two-device one, and a tensor group larger than any node is refused
outright — no arrangement of machines serves it, and reporting that as a capacity shortfall points
a person at a queue that will never clear.

**`placement.topology: single_domain` is honoured at both ends, and it means different things at
each.** The plane reads it when it chooses a cluster: a gang lands inside one of that cluster's
observed domains or the cluster is rejected. The operator reads the same field when it renders,
and pins every pod of a replica to one fabric — a node carrying no `nvidia.com/gpu.clique` is a
domain of its own, so nodes with no declared fabric between them are never combined. Without the
second half the first is decoration: the plane picks a cluster on the strength of a domain and the
cluster then spreads the replica over whichever machines are free.

## Demand — operator → hub

| Type | Payload |
|---|---|
| `deployment.observed` | phase, `reason`, `message`, replica counts, `replica_ids`, last error |
| `replica.observed` | which devices, readiness, engine version, the serving digest it came up on, restart count, observed generation |
| `replica.event` | started, ready, failed, OOM, preempted |
| `allocation.observed` | devices currently committed, and to what |
| `cache.observed` | artifact residency, per node, and eviction pressure |
| `component.observed` | rollout status per component |

**`deployment.observed` carries `replica_ids`, and it is how the plane forgets a replica while the
deployment still exists.** A replica that simply stops being reported — a rolled pod, a restarted
one, a deployment scaled down — has nothing else to remove its row. It stayed in the plane's
database for ever and kept counting towards the rollup, which is how a deployment of one came to
read three ready of one. The frame names this deployment's whole replica set on this cluster every
time it is sent, and the plane deletes the ones it does not name; the allocations those replicas
held cascade with them, which is the same leak on the device side.

This is a per-deployment claim rather than a snapshot, and that is what makes it safe without the
resync guard: absence from the list is evidence about one deployment on one cluster, and the frame
either arrived whole or did not arrive at all. `null` is an operator that predates the field and
prunes nothing; `[]` is a claim that there are none, which is what a scale to zero says. For the
same reason the operator must not suppress a `deployment.observed` whose counts are unchanged but
whose replica set is not — a pod replaced one-for-one changes no count and still needs the prune.

**A per-deployment claim cannot say that a deployment is gone, which is why there is a `demand`
resync as well.** Every path that removes a deployment from a cluster also removes the frame that
would have reported it: a pause, a delete, a teardown, or an operator restarted while the pod was
going away. The last frame sent then stays true for ever — replicas `ready` that are not running,
allocations `held` on accelerators nobody is using, and no later frame that could contradict
either. Nothing built on transitions can repair that, because the transition is precisely what was
not witnessed.

So the operator streams a `demand`-scoped resync of every replica and allocation the cluster
holds, and the plane prunes both against it, cluster-wide, under exactly the guard the `supply`
snapshot has: the stream declares its own length and completeness, and a snapshot that did not
provably arrive whole prunes nothing. The operator must not claim `complete` for a snapshot in
which any deployment could not be read — the frames that deployment would have contributed are the
ones whose absence would otherwise be read as "gone". An incomplete snapshot costs a stale row
until the next resync; a wrongly complete one deletes a running replica's allocation and offers its
accelerators to somebody else.

The two mechanisms overlap deliberately. `replica_ids` is cheap and immediate and keeps a serving
deployment's rollup honest between snapshots; the snapshot is the one that can shrink the plane's
picture, and is the only one that converges from an arbitrary starting state.

**`deployment.observed` carries `reason` and `message`, and the operator sends one on every
outcome — including the ones that are not errors.** A deployment spends most of its life before it
serves waiting for orderings to be satisfied: the weights have not arrived, the cache is still
fetching, no node advertises enough accelerators. Each of those is a correct, expected state and
none of them is a failure, so none of them belongs in `last_error`. Without them the plane sees a
deployment at `pending` for twenty minutes and can say nothing about which of six things it is
waiting on, while the operator knows exactly and says so in a log line in another cluster.

`reason` is a closed set owned by the operator, because a set the plane also writes into is a set
nobody can enumerate. `message` is free text for a person and is never parsed. Both are optional:
absent means the cluster is not distinguishing a sub-state, not that it is healthy.

The set is ordered: it is the sequence one revision of a deployment passes through, and a reader
who takes them in this order is reading the deployment coming up.

| `reason` | Phase | Means |
|---|---|---|
| `Accepted` | `pending` | The desired frame decoded and this cluster has begun reconciling it |
| `ArtifactNotDeclared` | `pending` | The artifact this deployment serves has not reached the cluster |
| `WeightsNotResident` | `pending` | The weights are not yet cached on this cluster |
| `WarmSetIncomplete` | `pending` | The cache is fetching, and this artifact is not done |
| `NoSchedulableCapacity` | `pending` | No node advertises enough accelerators of the class |
| `NoClusterWork` | `pending` | Held as declared state; `authority` is `observed` |
| `Rendering` | `pending` | Every ordering is satisfied; building the objects that will run it |
| `Applied` | `placed` | Rendered and applied; no replica has reported ready yet |
| `Progressing` | `degraded` | Some replicas are ready and some are not — filling in, or rolling |
| `ModelNotRegistered` | `degraded` | Every replica is ready and the frontend does not serve this deployment's model |
| `Serving` | `running` | Every desired replica is ready |
| `PodUnschedulable` | `placed` | A replica's pod exists and the scheduler cannot place it |
| `CrashLooping` | `failed` | A replica's container starts and exits repeatedly |
| `ContainerConfigInvalid` | `failed` | A replica's container cannot be created from its config |
| `ImagePullFailed` | `failed` | A replica's image cannot be pulled |
| `ComponentNotReady` | `placed` | A controller the cluster derived from ours reports not ready, and did not say it was refused |
| `ClusterRejected` | `failed` | A controller in the cluster refused the objects that were applied |
| `NoReplicasStarted` | `failed` | Applied, no replica ready past the grace window, and nothing said why |
| `PayloadUndecodable` | `failed` | The desired frame did not decode |
| `RenderFailed` | `failed` | The frame decoded and describes nothing renderable |

**`ModelNotRegistered` is the one shortfall every other signal calls healthy.** A frontend
registers a model by resolving the card its worker published, and a card it cannot resolve leaves
the frontend listening, passing its readiness probe and serving nothing. Every count is complete,
the graph reports `successful`, the agent forwards to it and the gateway prefers it — and the
frontend answers 404 to every request for the model. Nothing that counts pods can see this, which
is why the operator asks the frontend directly (`GET /v1/models`) rather than inferring it.

The question is put only to a frontend pod that is already ready, and only where one frontend is
rendered per model. An unanswerable question is not a negative answer: a frontend that cannot be
reached keeps the verdict its own readiness gives, because the plane withdraws a deployment it is
told is not running and a check that cannot reach the data path would take a serving fleet out of
the routing table. Only a frontend that answered, and did not name this deployment's model, is
reported.

The causes are not one bug. A tokenizer config that carries no chat template registers nothing; so
does a model card naming a filesystem path, on a runtime whose worker does not serve its own
metadata over HTTP and whose frontend has no access to that path. Both read identically from
here, and both are what this reason is for.

**`Accepted` and `Rendering` are sent once per revision, not once per pass.** Both are true for the
few milliseconds between one check and the next, while a reconcile re-runs every fifteen seconds
until it applies and every five minutes after. Sent on every pass they would alternate against
whatever the loop concluded next, and since the receiver files an event per transition, a healthy
deployment would accumulate a timeline of the same two rows forever. They are facts about an
instruction arriving, and a revision is what an instruction is — so the operator remembers how far
into this set it has reported for the current revision and never goes backwards within one.

**Everything between the apply and a running pod has a reason of its own, and `last_error` is
where the sentence goes.** `Applied` says a write to the cluster's API succeeded and the counts say
no replica is ready, and for a long time that pair was the whole vocabulary for the region between
them — so a deployment the API server had permanently refused, one whose image did not exist, and
one still loading a model all reported the identical `placed · 0 of 1 replicas ready`. The
operator now reads that region: the conditions on the objects it rendered, the objects the cluster's
own controllers derived from those, and the pods' waiting reasons. `ClusterRejected` is the one that
matters most, because the refusal is written on an object the plane never sees and is permanent —
the same spec is refused identically for ever.

`last_error` carries the code and the controller's own message, and is set only for a failure. The
waiting reasons above it are not failures and stay out of it, which is the rule that keeps
`last_error` meaning something. `message` keeps the replica counts and appends the diagnosis, so a
reader gets the number they looked for and the sentence they came for in one line.

**`ComponentNotReady` is not a failure and must not be sent as one.** A controller the cluster
derived from our objects reports `False` with `Deployment is not ready` for the whole of a normal
rollout — an image pulling, a model loading, a pod starting. Reported as `ClusterRejected` it made
the console draw `deployment.placed` in red beside the words "the cluster refused", which is a
phase that means it worked, a colour that means it broke, and a sentence that means neither; a
reader could not tell whether to wait or to act. It is `placed` because the deployment is applied
and a replica is still coming, and it is read *after* the pods, because a pod that is pulling or
unschedulable is the reason this summary exists.

**`NoReplicasStarted` is the reason for having no reason.** A deployment can be applied, produce no
pod, and draw no complaint from any controller — nothing is wrong anywhere, and nothing is running.
Reported as `placed` that is indistinguishable from starting up, and it stayed that way for as long
as the deployment existed. Past a grace window measured from the rendered object's creation, the
silence is itself reported as the failure. It is deliberately last: every diagnosis with a name of
its own is reported the moment it is seen, and this catches only what none of them explain.

**`Applied`, `Progressing` and `Serving` are three answers, not one.** `Applied` is a statement
about a write to the cluster's API and says nothing about whether a worker ever started; the other
two are read back off the running replicas. Collapsing them — reporting `Applied` for everything
after the apply — leaves a deployment that is serving indistinguishable from one that was written
and never came up.

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

**`replica.observed` carries `serving_digest`.** It is the `digest` off the
`deployment.desired.serving` this replica was rendered from, echoed back unchanged — the cluster
never computes it and never acts on it. It is what lets a replica, a benchmark run and a desired
frame name one configuration the same way, so a replica still running the previous configuration is
visible as such rather than inferred from an engine version that moves for other reasons. It
replaces `recipe_id`, which named an object that no longer exists and was mutable besides.

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

**`cache.observed` carries `reason` and `message`, on every state and not only on failures.** The
same argument as `deployment.observed`, one layer down. `state: "absent"` cannot distinguish a
download that has not started yet from one that nothing on the cluster is able to start, and the
second of those never resolves on its own: the operator fetches weights with the engine image of a
deployment that serves them, so an artifact no `deployment.desired.serving` on that cluster names an
image for has no downloader and waits for ever. The plane used to see only `absent` and a deployment repeating
`WeightsNotResident`, with the reason recorded nowhere it could show. Both fields are free text and
both are omitted when empty, so an operator that predates them decodes against a new plane and a new
operator against an old one.

Reasons the operator sets deliberately: `NoRuntimeImage`, `ArtifactNotDeclared`, `PrefetchPending`,
`Fetching`, `Resident`. The plane files each transition as a `cache.<state>` event against the
artifact, which is what puts the download on the timeline of every workload waiting on it.

## Operations

| Type | Direction | Payload |
|---|---|---|
| `operation.desired` | hub → op | typed: drain, cordon, reboot, device reset, cache warm, roll, upgrade, benchmark. Carries target, reason, requested_by, deadline |
| `operation.observed` | op → hub | phase, progress, result, failure |
| `operation.cancel` | hub → op | withdraws an operation already sent. Carries the operation id, reason, requested_by, requested_at |

Operations are the third kind of thing — imperative, long-running, able to fail. They are not
status fields on the objects they act upon, and modelling them as such is the mistake this
registry exists to prevent.

**Operations are outside the resync contract.** A full-resync snapshot is the authority for pruning
declared state, and an operation absent from one has not been withdrawn — it is not the kind of
thing a snapshot describes. Pruning against one would cancel work already half-done, so an
operation is accepted once, run to a conclusion, and reported. The consequence is that delivery is
at-least-once with no replay window: a receiver drops a redelivery of an operation it is already
running rather than starting a second pass over the same node.

**Cancellation is a second imperative, not a restatement of the first.** `operation.cancel` names an
operation the hub has already sent and asks for it to stop. It is a frame of its own rather than a
flag on `operation.desired`, for the reason above: re-sending the instruction would make the
withdrawal prunable by a resync, which is the property operations are kept outside the snapshot to
avoid. The operator answers on `operation.observed` with a terminal phase, exactly as it would for
an operation that ended by itself — the cancellation carries no reply of its own.

**A cancellation for an id the operator does not hold is not an error.** The operation may have
finished a moment before the frame arrived, been refused, or belonged to an operator process that
has since restarted. Cancellation is therefore at-least-once and idempotent, and a receiver that
has nothing to stop logs it and drops it rather than reporting a phase for work it never had. A
queued operation is cancellable as well as a running one: a ramp holds most of its steps in a
queue, and a withdrawal that reached only the step in flight would let the rest run against a
deployment somebody has already stopped measuring.

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

**What the fixture holds is AIPerf's shape, described and not asserted.** The corpus needs one
concrete `summary` to round-trip, so `benchmark.observed.json` carries the export AIPerf 0.4 writes
to `profile_export_aiperf.json` — verbatim, the file the operator lifts out of the job's log between
its own sentinels. AIPerf reports each distributional metric as an object of its own, keyed
`time_to_first_token`, `time_to_second_token`, `inter_token_latency`, `request_latency`,
`input_sequence_length` and `output_sequence_length`, each carrying a `unit` and `avg`, `min`,
`max`, `p50`, `p90`, `p95`, `p99` and `std`. The rates — `output_token_throughput`,
`output_token_throughput_per_user`, `request_throughput` — carry a `unit` and at least an `avg`,
and the counters `request_count` and `error_count` are bare numbers. None of that is a schema this
protocol imposes: a receiver validating a `summary` against it would be asserting the union the
paragraph above refuses. It is one producer's format, written down so a reader can be built against
something real rather than against an invention.

A `summary` is a whole export, and a metric it could not compute is `null` or absent rather than
zero. A reader that cannot resolve a path names the miss; it never reads an absence as a number.

**One run is one load point.** A `ramp` or a `sweep` is N runs of the same harness against the same
target, each its own `benchmark_id` and its own frame, and the ordering is the plane's — the frames
carry no step index because the run rows already hold one.
[`fixtures/samples/benchmark.ramp.json`](fixtures/samples/benchmark.ramp.json) is one such
expansion end to end: seven frames over concurrency 1 to 64, with the step at 32 missing two
metrics so a reader is exercised against a real absence. It sits under `samples/` rather than
beside the corpus because the corpus is one frame per registered type and this is a sequence of
frames of a type that already has its fixture.

## Resync

| Type | Direction | Payload |
|---|---|---|
| `resync.begin` | either | `scope` — `supply | demand | all` |

`reply_to` is the sole correlation between a `resync.begin` and its terminating `stream.end`. A
receiver handling concurrent resyncs of different scopes matches them on that and nothing else,
which is why `stream.end` carries no `scope` of its own.

A resync opens with `resync.begin` and closes with the ordinary `stream.end`; there is no
`resync.end`. Two gateways with identical payloads would be two ways to say one thing, and a
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
