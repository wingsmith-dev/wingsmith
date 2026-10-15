# Architecture

How Hangar is put together and why. This is the reference for the shape of the system: the
topology, the rules the object model is built on, the objects themselves, and the processes that
carry them. Individual choices, with the alternatives they beat, are recorded as ADRs in
[`../decisions/`](../decisions/); this document links to them where they settle a point.

---

## 1. What this is, and what it is not

Hangar is a control plane that serves **many models across several Kubernetes GPU clusters**:
cluster enrolment, device inventory, placement, model lifecycle, serving configuration, quota, and
operations, plus the gateway that routes inference requests to the cluster serving each model.
Inside each cluster, a model is served by rendering it into an **NVIDIA Dynamo** graph
(`DynamoGraphDeployment`); Dynamo, with vLLM, SGLang or TensorRT-LLM inside it, is the only serving
runtime Hangar drives today.

It is pre-release and built for one organisation operating its own clusters, run by a single
operator or a small team sharing one admin role. What is not built yet is listed in the
[roadmap](../roadmap.md); this document describes the system as it works today and says so where a
design reserves room for more.

Four scoping decisions frame everything below.

**It is standalone.** Hangar owns its own database, its own deployment and its own vocabulary. A
system that wants to sell or meter inference on top of it talks to it through its API.

**Inference serving only, for now.** Everything Hangar renders today is a long-running model
server. The object model reserves room for batch and gang-scheduled work (§11.2), but no renderer
for them exists.

**NVIDIA Dynamo inside the cluster.** The plane never sends Kubernetes manifests (§3.2), so a second
renderer is possible, but the operator renders only Dynamo graphs. A cluster has to run the Dynamo
platform, which the `cluster-setup` chart installs.

**The organisation running Hangar owns every cluster it manages.** No clusters belonging to a third
party, no air-gapped installs. This simplifies exactly one object (Pool, §5.1) and it is easy to
read too much into it (§2.2).

What it deliberately is not:

- **Not a device scheduler.** The scheduler inside each cluster picks cards. Hangar picks clusters.
- **Not a Kubernetes dashboard.** Showing an operator CRD trees and asking them to reconstruct
  intent is the thing being replaced.
- **Not a training or batch platform.** It does not run jobs to completion, and it does not preempt.
- **Not in the request path of the control plane.** Inference traffic goes through a separate data
  plane (§7) that keeps serving when the control plane is down, though a gateway that restarts
  during a control-plane outage cannot come back up (§7.1).
- **Not a per-cluster configuration repository.** A cluster is registered, handed a token and a
  `values.yaml`, and installs itself. There is no directory per cluster anywhere in the repository;
  the plane's registry is the list of clusters.

### 1.1 Where it sits among related projects

Most of the projects below work inside one cluster, or in front of endpoints that already exist.
Hangar's layer is the one above a set of clusters: which clusters a model runs on, what each one
is told to run, and how a request finds it.

| Project | What it does | Relation to Hangar |
|---|---|---|
| **NVIDIA Dynamo** (operator and Planner) | Serves a model inside one cluster: frontend, KV-aware router, disaggregated prefill/decode workers; the Planner scales workers against latency targets | Hangar renders into it. Each Hangar deployment becomes one `DynamoGraphDeployment` in one cluster |
| **llm-d** | Kubernetes-native distributed inference inside a cluster: vLLM, an inference scheduler on the Gateway API Inference Extension, disaggregation | An in-cluster alternative to Dynamo. Not a renderer Hangar has |
| **KServe** | Model-serving CRDs (`InferenceService`) per cluster, with autoscaling and many runtimes | Per-cluster serving. Hangar does not render KServe objects |
| **AIBrix** | Control-plane components for vLLM in one cluster: LLM-aware gateway, autoscaler, LoRA management, distributed KV cache | Same layer as Dynamo's own control plane, inside one cluster |
| **SkyServe** (SkyPilot) | Serves a model across clouds and regions by provisioning its own VMs or pods and load-balancing across them | Provisions compute itself; Hangar manages clusters you already run and leaves provisioning to you |
| **LiteLLM, Envoy AI Gateway** | API gateways in front of model providers: one API, keys, rate limits, cost tracking | Route to endpoints that already exist. Hangar's gateway is narrower (no billing, no provider catalog) and is fed by the plane's knowledge of what runs where |
| **Run:ai, KAI Scheduler** | GPU scheduling inside a cluster: quotas, fair share, preemption, for training and inference | Device scheduling, which Hangar deliberately leaves to the cluster. Hangar does not preempt or run training |

NVIDIA has signalled multi-cluster coordination for Dynamo. If that ships, the overlap with Hangar
is cross-cluster placement and routing. What Hangar adds on top of a single cluster's Dynamo today:

- **Clusters that only dial out.** No kubeconfig in a central database and no inbound port on any
  cluster, for the control stream and for inference traffic (§2, §7.2).
- **Adoption before management.** Models already running in a cluster are observed and can be put
  under management deliberately, without a restart (§6.4).
- **Tenants and endpoints across clusters.** One public model name, per-tenant hosts and API keys,
  per-tenant KV-cache isolation, and weighted or ordered routing to deployments on several clusters.
- **Fleet inventory per device.** Device identity and health from DCGM, a device-class table shared
  by plane and operator, and placement against it (§5).
- **Measurement of the serving path.** Conformance probes of an endpoint against a reference
  provider (`hgrconform`), load tests (`hgrbench`) and request-path measurement (`hgrtransit`).

---

## 2. Topology: hub and spoke, with an outbound agent

### 2.1 The shape

> **The control plane stores intent. The cluster stores truth. A cluster-resident operator
> reconciles the gap, and it always dials outbound.**

```
                         ┌──────────────── control plane ────────────────┐
console (ts) ──http──►   │  api (python) ◄──► Postgres ◄──► hub (python) │
hgr     (go) ──http──►   │                       ▲                 ▲     │
                         │                  reactor (python)       │     │
                         └─────────────────────────────────────────┼─────┘
                                                                   │ WebSocket, dialled outbound
                                                                   │
  caller ──https──► gateway (go) ◄── HTTP/2 tunnel, dialled outbound ── agent (go) ──► model frontend
                       │                                               operator (go)
                       └── reads the routing table from the api        └─ one of each per cluster ─┘
```

Each cluster runs two Hangar processes. The **operator** operates the cluster: it receives desired
state from the hub, renders it into Kubernetes objects, and reports what it observes back up. The
**agent** carries inference traffic: it dials the gateway and proxies requests to the model's
frontend inside the cluster. Neither the plane nor the gateway ever opens a connection into a
cluster.

This is where the field has converged: Run:ai, Open Cluster Management, Karmada, Rancher Fleet,
Argo CD's agent mode. The control plane never holds a kubeconfig and never connects inward.

The alternative, in which the plane holds credentials and calls each cluster's API server, is the
rejected design. It fails on five counts, and each gets worse as the number of clusters grows.

| # | Failure of the push model | Why it compounds |
|---|---|---|
| 1 | **Credential blast radius.** N clusters means N sets of cluster-admin credentials in one database. | The most valuable target in the system, and an audit finding waiting to happen. |
| 2 | **Reachability.** Inbound to `:6443` is a per-cluster firewall negotiation; outbound 443 works almost everywhere. | Linear in clusters, forever. |
| 3 | **Partition behaviour.** Under push, control-plane downtime means workloads stop being managed. Under pull, the cluster keeps scheduling, healing and serving; it only stops syncing. | The difference between "the control plane is down" and "inference is down". |
| 4 | **Scheduling latency.** A scheduler placing work over a WAN round trip to a remote API server is not a scheduler. | GPU bin-packing needs local node state at millisecond scale. |
| 5 | **Rollout blast radius.** A bad reconcile loop in the plane hits every cluster at once; an agent rollout is staged per cluster. | Unbounded against bounded. |

### 2.2 Owning the clusters does not change the answer

It is tempting to read "we own every cluster" as licence to go back to push. It is not. Ownership
removes reason 2 and only reason 2. Credential blast radius, partition behaviour, scheduling latency
and staged rollout are properties of the architecture, not of who owns the hardware. Ownership makes
push possible; it does not make it better.

What ownership does buy is a simpler enrolment story (an internal bootstrap rather than a
customer-facing join flow) and the ability to declare provisioning intent for node pools (§5.1).
Even so, owning a cluster does not mean owning the network it sits in, which is a second reason the
agent and the operator both dial out.

### 2.3 Transport

"Messaging, operator, or direct API calls" is a false choice. The operator is the answer; messaging
is a detail of how it syncs. The real choice was narrower:

- **An operator-initiated bidirectional stream.** One connection, request/response plus server push,
  straightforward TLS, tractable backpressure and resume. This is what Hangar uses.
- **A message bus.** Better for fan-out to many independent consumers, at the cost of making a broker
  a hard dependency of cluster management. If a bus ever appears it is an internal control-plane
  concern, not the plane-to-cluster wire.
- **Git as the sync medium.** Auditable and good for configuration, wrong for anything that needs
  fast feedback or dynamic placement. Ruled out.

The stream is a WebSocket carrying JSON envelopes rather than gRPC
([ADR 0001](../decisions/0001-websocket-json-over-grpc.md)). The cost of that choice is that the Go
and Python types are written by hand; the fixture corpus in `libs/packages/contract/fixtures` is what
keeps them from drifting. A message type with no fixture is not part of the contract.

---

## 3. Architectural principles

Four rules the object model is downstream of. Breaking any of them is what turns a control plane
into a mess in its second year.

### 3.1 Every object is declared, observed, or an operation

- **Declared**: the plane writes it, the cluster obeys. It lives in the plane's database and syncs
  down.
- **Observed**: the cluster writes it, the plane reads it. It syncs up and is denormalised into
  Postgres, so the console and placement never fan out across N clusters.
- **Operations**: imperative, long-running, and able to fail. Neither of the above.

The third kind is the one that gets skipped. **Drain is not a field on a node.** It is cordon,
evict, wait for in-flight requests, confirm, and possibly time out and force. It has a lifecycle, an
actor, a reason and a failure mode. Modelled as `node.status = 'draining'`, there is no answer to
"why is this stuck", "who did this" or "how long has it been", and the same lesson arrives again
with GPU reset, node reboot, cache warm and rolling update. One `Operation` object with a typed
payload covers all of them.

The observed half is not a mirror of the declared half. Hangar never declares a Node. Observed
objects also get their identity from the side that observes them: the cluster mints the id of a
node, device or topology domain when it first sees it, and the plane validates the shape and never
re-mints it, because the plane cannot name a thing it has not heard of yet
([ADR 0002](../decisions/0002-clusters-mint-observed-ids.md)).

Keeping the three apart is what lets the plane answer *what was asked for*, *what is true* and *what
is being attempted* as separate questions. A cluster that is `active` because that is what was
declared, and unreachable because that is what is observed, shows both; neither overwrites the
other.

### 3.2 The wire carries domain objects, never Kubernetes manifests

The plane does not send Kubernetes Deployments. It sends Hangar's own `Deployment`, `Pool`,
`Operation`, and the operator expands them into whatever Kubernetes objects it currently favours.

This is the most important structural decision in the system. The Kubernetes-level implementation
can change completely (a new serving runtime CRD, a different engine, eventually something that is
not Kubernetes) without the plane learning anything. All knowledge of Kubernetes stays on the cluster
side, which is also what lets the fleet be non-uniform: the operator absorbs the differences.

### 3.3 Scheduling splits into global and local

**Cluster selection is global, coarse and infrequent**: capacity, device class, region, residency,
cost. It lives in the plane. **Device selection is local, hot and frequent**: bin-packing, topology,
preemption. It lives in the cluster.

Global decisions are cheap and rare; local decisions are expensive and constant. A plane that picked
devices would need an accurate live model of every card in every region and would be wrong
constantly. Collapsing the split in either direction brings back failure 4 from §2.1.

### 3.4 The data plane never touches the control plane

Inference traffic, logs, metric streams and model weights flow directly or through the edge. None of
them pass through the control plane.

If any of them did, **the control plane's availability would become the inference SLA**. A control
plane is allowed to be down for an hour in a way an endpoint is not. The plane says where things go;
it never carries them. The gateway is the one Hangar process in the request path, and it is built to
keep serving from the last routing table it loaded when the plane is unreachable. It does depend on
the plane to start: a gateway that restarts during a plane outage cannot load a table and stays
down (§7.1).

---

## 4. The object model in one view

Four groups. Most of the design is in the seams between them.

| Group | Objects | Owner |
|---|---|---|
| **Supply**: what capacity exists | Cluster, Node, Device, TopologyDomain, Pool | cluster observes; Cluster and Pool also declared |
| **Demand**: what should be running | Artifact, Workload, Deployment, Replica, Endpoint | plane declares; Replica observed |
| **Binding**: where they meet, and the rules when they cannot | Tenant/Project, Quota, PriorityClass, Policy, Allocation | plane declares; Allocation observed |
| **Machinery**: what makes it operable | Component, Operation, Event, CacheEntry, Credential | mixed |

The HTTP API that exposes these is specified in `libs/packages/contract/API.md`; the console and
`hgr` are two clients of the same surface ([ADR 0004](../decisions/0004-http-api-cli-admin-reconciliation.md)).

---

## 5. Supply

| Object | Kind | Holds |
|---|---|---|
| **Cluster** | declared registry, observed state | enrolment identity, region, residency, capabilities, operator version, reachability, sync state |
| **Node** | observed | membership, conditions, kernel/driver/kubelet versions, cordon state |
| **Device** | observed | per-accelerator identity, health, ECC and throttle state, memory, MIG profile, current allocation |
| **TopologyDomain** | observed | which devices share an NVLink/NVSwitch fabric, PCIe root, or NUMA node |
| **Pool** | declared | provisioning spec: device class, node shape, min/max, labels and taints |

### 5.1 The schedulable unit is the accelerator, not the node

`gpu_count: int` on a node cannot express "seven of eight healthy". It cannot say which GPU is
throttling, which has ECC errors, or which is running what. Every capacity question that matters is
per device, so Device is a first-class object and node-level GPU counts are a derived convenience.

Kubernetes does not know a GPU's UUID; node labels say how many cards there are and what model, not
which cards. Device identity therefore comes from the DCGM exporter the cluster already runs, which
the operator reads inside the cluster
([ADR 0007](../decisions/0007-device-identity-comes-from-the-dcgm-exporter.md)). Vendor strings map
to a Hangar device class through one table, `libs/packages/contract/devices.json`, read by both the
operator and the plane.

**Pool is declared**, and this is the one place where owning the hardware changes the design. If
Hangar managed a cluster it did not provision, nodes would simply appear and a declared pool with a
min and max would be a lie; Pool would have to degrade to a label selector over observed nodes. Since
the operator of a Hangar install owns its clusters, Pool is a genuine provisioning spec. If that
assumption ever changes, this is the object that breaks first.

### 5.2 Topology is an object, not an annotation

Eight GPUs in one NVLink domain and eight scattered across four chassis are the whole performance
story for any model large enough to need tensor parallelism. It is also the thing that cannot be
retrofitted: schedulers that started with `gpus: int` and added topology later ended up with a
second scheduler beside the first.

With devices modelled with topology edges from the start, "eight GPUs in one fabric" is a query
over observed state. Placement uses it to choose only clusters where a workload that declares a
topology requirement fits inside one domain; which devices it lands on stays the cluster
scheduler's job. Added later, this would have been a rewrite.

### 5.3 Heterogeneity is a scheduling constraint

How a node joins a cluster is an implementation detail. Heterogeneity within a cluster is not: you
cannot tensor-parallel across an A100 and an H100. Two consequences follow, both structural:

1. **A workload expresses a device-class requirement**: not "8 GPUs" but "8 devices of class X with
   at least Y GB".
2. **A Pool can carve a homogeneous slice out of a mixed cluster**, so that placement has something
   coherent to bind against.

Neither is expensive if designed in. Both are painful once placement assumes devices are fungible.

---

## 6. Demand

| Object | Kind | Holds |
|---|---|---|
| **Artifact** | declared | immutable, content-addressed weights: digest, size, format, licence, one or more locations |
| **Workload** | declared, plane-only | what you asked to run, globally: the artifact and a reproducible way to serve it (engine and version, quantization, TP/PP/EP, context length, KV and prefix cache configuration, engine arguments, the resulting device shape and class), plus kind (only `service` is rendered; `task` and `gang` are reserved), total scale bounds, priority, and which clusters it may use. Has many Deployments |
| **Deployment** | declared | one workload on one cluster, with **that cluster's share** of the scale. The unit the operator reconciles, carrying the serving configuration inlined |
| **Replica** | observed | a running instance on named devices, with readiness and engine version |
| **Endpoint** | declared, plane-only | the stable public name clients call; resolves to N deployments with weights, failover and canary |

### 6.1 Artifact and Workload are two levels, and neither is "the model"

The instinct is "model, then deployment". It breaks at both ends.

If "model" means "a Hugging Face identifier", then a fine-tune output, private weights, a quantized
derivative produced in-house, and a LoRA adapter are none of them models. All four are
**artifacts**. Content-addressing them gives the cache layer something coherent to name (you cache
artifacts; "caching a model" is not a well-formed statement) and makes a LoRA adapter an artifact
attached to a running deployment rather than a special case in the serving path.

The level above is a fully specified, reproducible way to serve some weights: engine and version,
quantization, parallelism, context length, cache configuration, engine arguments, and the device
shape that falls out of them. Llama-70B at FP8/TP2/32k and the same weights at BF16/TP4/128k have
different hardware footprints, different costs and plausibly different clusters. **They are two
Workloads over one Artifact**, told apart by their `serving_digest`, a hash of exactly the fields
that decide what process runs.

That configuration used to be an object of its own, the Recipe. It was deleted: the sharing it
offered was being reconstructed by comparing fields rather than used, and it cost an ordering rule on
the wire and a resume race. The configuration now lives on the Workload and reaches the cluster
inlined on `deployment.desired.serving`
([ADR 0014](../decisions/0014-recipe-is-deleted-serving-config-lives-on-the-workload.md)). If a named,
sellable configuration is ever wanted, it is a thin catalog object pointing at a digest.

**Context length lives on the workload**, not on the model, because it determines the device shape.
Putting it on a catalog entry forces one way of serving everything.

A product-facing "Model" (family, description, licence, a featured flag) is catalog metadata that
groups artifacts. It is a UI concern, not a scheduling one, and it is not allowed to become the root
of the demand hierarchy.

### 6.2 Weight cache and KV cache are unrelated

"The cache layer" is two different things, and collapsing them produces an object where half the
fields are desired state and half are runtime telemetry.

| | Weight cache | KV / prefix cache |
|---|---|---|
| What | model files on disk or NVMe, shared across replicas | attention state, per replica or in a cluster tier |
| Cadence | slow-moving | per request |
| Kind | **declared**: a warm-set per cluster with an eviction policy; residency is observed as `CacheEntry` | **serving configuration on the workload** |
| Coupling | artifact distribution | routing: a KV-aware router only works if it knows cache residency |

The warm-set is a declaration next to Demand, residency is a Machinery observation, and KV
configuration is a field of the workload's serving block.

### 6.3 Workload is the global umbrella; Deployment is that workload on one cluster

Workload is the **parent** of Deployment, not its supertype: a workload *has* deployments, one per
cluster it runs on ([ADR 0010](../decisions/0010-workload-is-the-global-umbrella.md)).

| | Workload | Deployment |
|---|---|---|
| Scope | global | one cluster |
| Kind | declared, plane-only, never on the wire | declared, sent to that cluster's operator |
| Carries | artifact and serving configuration, adapters, overrides, labels, kind, priority, preemptible, gang size, **total** min/max/desired scale, deadline, topology requirement, placement constraints | cluster, admission phase and reason, queued/admitted/placed timestamps, **this cluster's share** of the scale, generation, authority, adoption provenance |
| Created by | a person, over the API | placement, when it expands a workload |
| Cardinality | one per thing you asked to run | one per cluster the workload occupies |

The canonical sentence: *run gpt-20b in asia and in europe* is one Workload, two Deployments, one
Replica each.

An operator only ever sees its own cluster, so a Deployment is still exactly the object it receives.
The replica count it obeys is this cluster's share, never the workload total, and the operator never
sums or re-derives it.

**Splittability is a property of kind.** A `service` may be split across clusters. The reserved
kinds carry their own rule for when they are built: a `gang` may not be split, because its ranks
must be co-scheduled, so it would always have exactly one Deployment. Nothing below Workload is meant
to branch on kind except the operator, deciding which Kubernetes object to render, which is §3.2
working as intended. Today the operator renders `service` only (§11.2).

**Scale is declared as a total and derived per cluster.** Workload carries total min/max/desired,
optional per-cluster floors, and an optional cap on how many clusters it spreads over; placement
derives each Deployment's share as a fixed replica count. The maximum is reserved for an autoscaler
that does not exist yet ([roadmap](../roadmap.md)). The default fill strategy is **pack** rather than
spread, so spreading is opt-in.

**Placement constraints are Policy, not new fields.** Policy already carries allowed regions,
residencies, device classes and allowed or denied clusters (§8). Explicit cluster pinning survives as
a deliberate escape hatch and attaches to the Workload.

**Quota and admission stay per cluster** and therefore stay on Deployment: quota is keyed by
(project, cluster, device class), and a workload can be admitted in one cluster while queued in
another. There is no global admission phase, because there is nothing global to admit against.

**Scaling is an operation, not a field write** (§3.1). `workload.scale`, `workload.pause` and
`workload.drain` are long-running and can fail; a scale can sit pending on quota indefinitely.

### 6.4 Adoption and authority

Hangar is pointed at clusters that are already serving, with models resident in GPU memory and
traffic on them that the plane never declared. Importing them as declared state would restart every
one of them the first time the reconciler ran.

So the operator reports what it finds as an observation (`workload.discovered`), and tracking it in
the plane is a decision to observe, not to manage
([ADR 0006](../decisions/0006-adoption-is-a-decision-to-observe.md)). Whether the plane may act on a
deployment is a separate, explicit field, `authority: observed | managed`, on the deployment
([ADR 0009](../decisions/0009-authority-is-a-mark-not-a-consequence-of-adoption.md)). An `observed`
deployment is recorded and checked for drift, and nothing is ever written to the cluster for it.
Arguments read off a running process are kept as `imported_args`, separate from the declared
`engine_args`, so becoming `managed` is a reviewable act rather than a guess about which captured
values somebody meant.

---

## 7. The data plane

The gateway, the tunnel and the agent are how a request reaches a model. None of them is part of the
control plane, and while they are running none of them needs it to be up. The exceptions are at
start-up: the gateway needs the plane to load its first routing table, and an agent that redials
needs the gateway to verify its token, which the gateway can answer from cache for up to an hour
without the plane.

### 7.1 Gateway

`libs/gateway` takes an OpenAI-compatible request, reads `model` from the body, and forwards it to a
cluster currently serving that model. It is one hop. The plane derives the routing table; the gateway
keeps a copy in memory and refreshes it on a timer, so nothing is joined per request.

- **The plane is down**: the table already loaded keeps serving. What it is built from changes on the
  order of days, and a control-plane outage must not stop traffic to models that are running fine.
- **It boots with no table**: it refuses to start. Coming up empty would answer everything with
  `model_not_found`, which reads as an outage of the models rather than the misconfiguration it is.
  The table is held in memory only, so this includes a gateway restarted while the plane is down:
  a restart, eviction or scale-up during a plane outage leaves that replica out until the plane
  returns. Persisting the last table is on the [roadmap](../roadmap.md).
- **An endpoint has no upstreams**: `503 no_capacity`, never `404`. "No such model" and "nowhere to
  send it" are different answers and a caller acts differently on each.

The gateway does not meter, bill, or pick replicas. Whoever sells inference already has a place where
metering happens, and the serving runtime inside a cluster routes on queue depth and KV-cache
residency, state that does not and should not leave the cluster.

It does identify tenants. Which listener accepted a connection is the trust primitive: the internal
listener has no ingress, so reaching it is the proof; on the public listener the `Host` header names a
tenant, and a tenant that declares `auth: api_key` must present a key belonging to it. Another
tenant's model reads as `404`, never `403`
([ADR 0016](../decisions/0016-the-listener-is-the-guard-tenant-identity-replaces-the-shared-token.md)).

**The gateway is one failure domain.** It is a single deployment, wherever it runs, so if every
replica is down every model is unreachable through Hangar, in every region. Run several replicas
across zones; regional gateways are not designed yet. Its internal listener has no authentication
and must be kept off any ingress and behind a NetworkPolicy
([deploying the plane](../operations/deploy-plane.md)).

Because it sees every request, including those that never reach a cluster, the gateway is the one
honest source of a failure rate. It counts each request under a closed set of outcomes, keeping
"caller over its own limit" and "this hop shedding load" apart even though both are `429`.

### 7.2 Tunnel and agent

Nothing dials into a GPU cluster, not even the gateway. The agent (`libs/agent`) dials out to the
gateway, authenticates with the cluster's enrolment token, and then the roles reverse: the agent runs
the HTTP/2 server on that connection and the gateway runs the HTTP/2 client. Both ends of that wire
live in `libs/packages/tunnel`, a module of its own because neither side owns it.

The agent is given one gateway address and finds every gateway replica behind it: each accepted
handshake names the replica that took the dial and how many there are, so the agent keeps redialling
until it covers them all. A cluster is never configured with the shape of the control plane, and the
gateway can scale without an edit reaching any cluster.

The agent and the operator are separate processes with separate connections. If a request needed
the operator's sync connection, control-plane uptime would be the inference SLA again.

### 7.3 One frontend per model

Inside the cluster, the operator renders a frontend for each model as part of that model's
deployment, rather than one shared frontend for the cluster. The gateway forwards the deployment id
it resolved as `X-Hangar-Deployment-Id`, and the agent maps it to the frontend Service labelled with
that id ([ADR 0013](../decisions/0013-one-frontend-per-model-routed-by-a-forwarded-deployment-id.md)).
It does not poll each frontend's `/v1/models`: two deployments of the same model on one cluster, a
canary for instance, advertise the same name, and routing on it would silently collapse a weighted
rollout. The frontend's router mode is declared on the workload rather than rendered as a constant
([ADR 0015](../decisions/0015-the-frontend-router-is-declared-on-the-workload.md)).

---

## 8. Binding

| Object | Kind | Holds |
|---|---|---|
| **Tenant / Project** | declared | the unit quota attaches to |
| **Quota** | declared | guaranteed floor plus over-quota fair share, per project per cluster per device class |
| **PriorityClass** | declared | a priority and a `preemptible` flag; recorded, not yet acted on |
| **Policy** | declared | placement constraints: region, residency, device class, cost ceiling, allowed clusters. Attachable to tenant, endpoint, workload or cluster |
| **Allocation** | observed | devices currently committed to a replica |

Quota is the primitive this group is designed around: a guaranteed floor per project, cluster and
device class, checked by placement at admission, with room for an over-quota share of idle
capacity. A plain namespace quota is not enough because it is a hard cap per cluster, with no
notion of a device class or of the fleet.

A second, unrelated quota, **ServingQuota**, declares an endpoint's committed request rate, token
rate and concurrency. It is stored and shown but not yet enforced by the gateway.

---

## 9. Machinery

| Object | Kind | Holds |
|---|---|---|
| **Component** | declared bundle, observed rollout | what is installed in a cluster and at what version: the operator, the agent, serving runtime, exporters, collectors, cache tier |
| **Operation** | operation | typed, long-running, failable: drain, cordon, reboot, device reset, cache warm, rolling update, component upgrade. Carries target, requester, reason, phase, deadline, result |
| **Event** | observed | node joined, device degraded, load failed, OOM |
| **CacheEntry** | observed | residency of an artifact on a cluster or node, against the declared warm-set |
| **Credential** | declared | registry pulls, Hugging Face tokens, artifact-store access; scoped, never cluster-admin |

**Observability is a Component, not a subsystem.** The collection logic lives inside each cluster,
which makes it the same kind of thing as the serving runtime and the exporters: something installed,
versioned independently, configured per cluster, and watched as it rolls out. Give observability a
bespoke path and the cache tier will want one next, and the problem of installing and versioning a
thing in a cluster gets solved three times.

### 9.1 Telemetry

Metrics are part of the data plane: each cluster's collector pushes over `remote_write` into a
metrics store, and none of it passes through the hub. The plane reads that store beside the API at
request time, and the browser never queries it directly
([ADR 0003](../decisions/0003-telemetry-is-read-beside-the-api-not-through-the-hub.md)).

- Metric names are Hangar's own and vendor-neutral. The query dialect sits behind a `Backend`
  interface, so a second store is a new implementation rather than a refactor, and every query carries
  a cluster matcher ([ADR 0008](../decisions/0008-metrics-are-answered-by-a-backend-not-by-promql.md)).
- Each cluster's metrics label is declared on its registry row
  ([ADR 0011](../decisions/0011-a-cluster-carries-its-own-metrics-label.md)), and the plane is the
  source of the credentials the write path accepts
  ([ADR 0012](../decisions/0012-the-plane-is-the-source-of-the-metrics-write-htpasswd.md)), so
  enrolling a cluster touches nothing outside the console.
- Measurement never joins an inventory response. A metrics store that is down cannot make a node page
  fail. Absent is never zero: a missing series drawn as 0% reads as an idle GPU, which is the one wrong
  answer that looks right.

---

## 10. The processes

### 10.1 The control plane

`libs/plane` is one Python package that runs as three processes over one Postgres database.

- **api**: the HTTP API, request/response, serving the console and `hgr`.
- **hub**: terminates the long-lived operator streams, with resume and backpressure.
- **reactor**: derives alerts on its own loop, under a lease, and reconciles the derived
  configuration the fleet needs.

They are separate because they fail differently. The api is stateless request/response; the hub
holds N persistent sessions.

Inside the package three concerns are kept apart. **Modules** (`app/modules/`) are one package per
object and the only place declared state is written. **Ingestion** (`app/ingestion/`) is the only
writer of observed state: high-frequency, idempotent, last writer wins per cluster. **Placement**
(`app/modules/placement/`) writes nothing; it takes a snapshot of observed state and returns a
decision, which makes it testable without a cluster. A module whose object is observed has no write
methods at all.

**The hub scales by shedding session state rather than sharing it**
([ADR 0005](../decisions/0005-the-hub-scales-by-shedding-session-state.md)). Cursors and the replay
buffer stay in one pod's memory. Reconnecting to the same pod resumes exactly; reconnecting to a
different one costs a full resync, which is expensive but always correct and never a silent gap. The
one fact shared in Postgres is which hub session currently owns a cluster's stream, claimed atomically
at `hello`. A displaced session notices by polling its own claim and closes. Correctness does not
depend on sticky routing.

### 10.2 The operator

`libs/operator` runs once per cluster and has two loops, and the split matters.

- **Sync loop**: stream to CRDs. Applies desired state from the plane into custom resources and ships
  observed state back up.
- **Reconcile loop**: CRDs to cluster objects.

The CRDs are the durable local copy of desired state. That is what lets a cluster survive a plane
outage: if the operator held desired state only in memory, a restart during a partition would make the
cluster forget what it is supposed to be running, which would forfeit the reason for an outbound
architecture. The resume cursor is persisted the same way, in a `StreamCheckpoint` resource.

Backpressure is fixed by the protocol, not configurable: when the outbound queue is full, a newer frame
replaces the oldest one with the same type and subject, because observed state is a snapshot of the
present and falling behind should cost resolution rather than correctness. Events are the exception
and are dropped from the tail, with the loss counted. Liveness never depends on the hub session; a
partitioned operator is healthy, because the cluster is meant to keep working.

The connection lifecycle, envelope, resume and backpressure rules are specified in
`libs/packages/contract/PROTOCOL.md`, and every message type in `MESSAGES.md`.

### 10.3 Shipping

The control plane is a pet; inference clusters are cattle, and they ship differently. The plane and
the gateway are images you run yourself, against a Postgres you operate and back up; there is no
chart for them yet ([deploying the plane](../operations/deploy-plane.md)). The operator and the
agent are images each cluster pulls through the `cluster-setup` Helm chart (`libs/chart`) on that
cluster's own `helm upgrade`. A cluster is created by registering it, which mints its enrolment token
and renders its values, and then installing the chart in it.

---

## 11. Decisions that shaped the model

### 11.1 Endpoint and Deployment are separate objects

**Split.** The cost is a second object in the simple case, which the design answers by creating the
Endpoint alongside the first deployment so the simple path stays one call. **An Endpoint never syncs
to a cluster**: it is plane-only, and it is what the gateway
reads.

Merging them gives one object that is globally unique but reconciled by N clusters at once, which is
two writers on one record. The lifetimes also differ by orders of magnitude: an Endpoint is a contract
whose name clients depend on, and a Deployment lives only as long as its workload is placed on that
cluster: a serving configuration edit or engine upgrade updates it in place (its generation goes up),
and a drain or re-placement deletes it. And the indirection exists either way, because
the gateway needs "public name to weighted upstreams" regardless; split, it is a first-class object
that can be versioned, audited and exposed.

| | Merged | Split |
|---|---|---|
| Simple case | one object | two, mitigated by auto-create |
| Same name from several clusters | the routing table lives in gateway config anyway | native |
| Canary / blue-green | mutate in place, no traffic split | native |
| Rollback | the previous version is gone | the old deployment survives until cutover |
| Per-tenant private routes | duplicate the deployment | many endpoints, one deployment |
| Renaming and aliasing | breaks clients | the endpoint is the stable name |

The only real case for merging is certainty that it will be one cluster per model, with no canary,
permanently. That does not survive a second region.

### 11.2 Inference first, with room reserved for other workloads

Hangar serves models. It does not run training or batch jobs, and it does not preempt anything.
The object model was nevertheless shaped so that adding them later does not mean rewriting
placement, because **serving-only placement and queue-based placement are different schedulers**,
and retrofitting a queue onto a reconciler that assumes "desired replicas, always admitted" is the
expensive part.

A service runs forever, scales on traffic and has an Endpoint. A training or batch job queues, runs
to completion, needs gang scheduling (all N devices or none) and has a deadline. Four things were
put in place for that, and this is how far each one goes today:

| Accommodation | Today |
|---|---|
| **One Workload root with a kind discriminator**, `service \| task \| gang`, and one placement path | Only `service` is rendered. The operator has no renderer for `task` or `gang`, so those kinds never reach a cluster. |
| **An explicit admission phase**: `pending → admitted → placed → running` | Built, and used by serving: admission succeeds unless quota refuses it. |
| **PriorityClass on every workload** | Recorded. Nothing acts on priority or on the `preemptible` flag, and placement does not count devices held by preemptible work as available. |
| **Atomic allocation inside one topology domain** | Built in placement for workloads that declare a topology requirement, which multi-node tensor parallelism needs for serving anyway. |

The motivation for wanting both kinds of work in one system is a mixed fleet: batch work fills the
capacity serving leaves idle and yields when serving scales up. That depends on task and gang
rendering and on preemption, all of which are on the [roadmap](../roadmap.md) and none of which is
built.

### 11.3 Deferred on purpose

Real work that does not constrain the object model:

- **Queueing policy**: backfill, fair-share ordering, deadline scheduling. The queue's existence is
  structural; its policy is not.
- **Checkpointing.**
- **Elastic resize** of running jobs.

---

## 12. The minimal core

Six objects make a working single-tenant, multi-cluster inference control plane:

> **Cluster · Device · Artifact · Workload · Deployment · Endpoint**

Workload and Deployment count as one entry in spirit (you create the first and placement creates the
second), but they are two rows and two modules, and the seam between them is §6.3.

The rest arrive on a trigger rather than a schedule:

| Add | When |
|---|---|
| Project + Quota | the second tenant |
| Operation | the first drain performed in anger, realistically week one |
| Pool | the first time capacity is provisioned rather than hand-built |
| TopologyDomain | before the first multi-node model, and it is in the Device model from the start regardless (§5.2) |
| Component | the second thing installed in a cluster |

---

## 13. Open questions

1. **Where does a product-facing Model catalog live?** §6.1 treats it as UI metadata grouping
   artifacts. If it becomes the unit of pricing and public listing, it acquires weight it may not
   survive as a view.
2. **Multi-cluster Endpoint semantics.** The gateway already splits by weight or fails over in a
   declared order to the first cluster it holds a tunnel to. Failure detection beyond that, the
   cutover mechanism, and whether the gateway or the plane owns it, are unresolved.
3. **Where does autoscaling decide?** Traffic-driven replica count is a hot signal that lives near the
   gateway, but scale bounds are declared in the plane. The split needs the same care as §3.3.
4. **Enrolment identity.** Each cluster authenticates with a token minted for it alone; how that
   credential is rotated without a gap is not yet specified.
5. **Regional gateways.** One gateway deployment is one failure domain for all inference. Whether
   gateways should be per region or per cluster, and how the routing table is scoped to each, is
   not designed.

---

## 14. Known limitations

Behaviour that is true today and worth knowing before you depend on it. Unbuilt features are in the
[roadmap](../roadmap.md); security limitations are also listed in [SECURITY.md](../../SECURITY.md).

- **Editing a disaggregated workload's roles or device shape reloads every worker.** Dynamo hashes
  one worker spec across every worker component in the graph, so changing a role or reshaping the
  devices renames both roles' objects and every worker reloads its weights, which can take tens of
  minutes for a large model. The rollout keeps the old generation up while the new one starts, so
  the peak device need is the old footprint plus the new one; on a cluster sized for one of them the
  new generation waits for devices the old one has not released. The API returns this cost with the
  edit (`unavailability`) rather than refusing it.
- **The gateway cannot start while the plane is down** (§7.1), and it is one failure domain for
  inference everywhere.
- **The metrics `cluster` label is not authenticated.** Any cluster with write credentials can write
  series under another cluster's label
  ([ADR 0012](../decisions/0012-the-plane-is-the-source-of-the-metrics-write-htpasswd.md)).
- **One admin role.** Human access is a single operator with full rights; there are no per-user
  accounts, roles or SSO.
- **One serving runtime.** Clusters must run NVIDIA Dynamo, and workloads are rendered only as
  Dynamo graphs.
- **The `hangar.dev` API group is interim.** CRDs are `v1alpha1` with no conversion, and the group
  name may change before the first release.

---

## Decision records

| ADR | Decides |
|---|---|
| [0001](../decisions/0001-websocket-json-over-grpc.md) | The plane-operator stream is WebSocket + JSON, kept honest by fixtures |
| [0002](../decisions/0002-clusters-mint-observed-ids.md) | Clusters mint the ids of the objects they observe |
| [0003](../decisions/0003-telemetry-is-read-beside-the-api-not-through-the-hub.md) | Telemetry is read beside the API, never through the hub |
| [0004](../decisions/0004-http-api-cli-admin-reconciliation.md) | One HTTP API for the console and the CLI |
| [0005](../decisions/0005-the-hub-scales-by-shedding-session-state.md) | The hub scales by shedding session state |
| [0006](../decisions/0006-adoption-is-a-decision-to-observe.md) | Adopting a running workload is a decision to observe it |
| [0007](../decisions/0007-device-identity-comes-from-the-dcgm-exporter.md) | Device identity comes from the DCGM exporter |
| [0008](../decisions/0008-metrics-are-answered-by-a-backend-not-by-promql.md) | Metrics are answered by a backend, not by PromQL |
| [0009](../decisions/0009-authority-is-a-mark-not-a-consequence-of-adoption.md) | Authority is an explicit mark on the deployment |
| [0010](../decisions/0010-workload-is-the-global-umbrella.md) | Workload is the global parent of Deployment |
| [0011](../decisions/0011-a-cluster-carries-its-own-metrics-label.md) | A cluster carries its own metrics label |
| [0012](../decisions/0012-the-plane-is-the-source-of-the-metrics-write-htpasswd.md) | The plane is the source of the metrics write credentials |
| [0013](../decisions/0013-one-frontend-per-model-routed-by-a-forwarded-deployment-id.md) | One frontend per model, routed by a forwarded deployment id |
| [0014](../decisions/0014-recipe-is-deleted-serving-config-lives-on-the-workload.md) | Recipe is deleted; serving configuration lives on the Workload |
| [0015](../decisions/0015-the-frontend-router-is-declared-on-the-workload.md) | The frontend router is declared on the Workload |
| [0016](../decisions/0016-the-listener-is-the-guard-tenant-identity-replaces-the-shared-token.md) | The listener is the guard; tenant identity replaces the shared token |
| [0017](../decisions/0017-video-input-is-bounded-in-decoded-bytes-not-frames.md) | Video input is bounded in decoded bytes, not frames |
| [0018](../decisions/0018-the-inference-tunnel-is-our-own-http2-role-reversal.md) | The inference tunnel is our own HTTP/2 role reversal, with its threat model |
