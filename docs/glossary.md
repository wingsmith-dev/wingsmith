# Glossary

Terms used across Hangar's code and docs, in alphabetical order. The
[architecture overview](architecture/README.md) explains how they fit together.

**Admission.** The phase a deployment passes through before it is placed:
`pending → admitted → placed → running`. For serving, admission succeeds immediately unless quota
refuses it.

**Adoption.** Recording a workload the operator found already running in a cluster, without taking
control of it. See *Authority* and [ADR 0006](decisions/0006-adoption-is-a-decision-to-observe.md).

**Agent** (`hangar-agent`, `libs/agent`). The per-cluster process that dials out to the gateway and
carries inference requests to model frontends inside the cluster. Not the operator.

**Allocation.** Observed: the devices currently committed to a replica.

**Artifact.** Immutable, content-addressed model weights (a base model, a fine-tune, a quantized
derivative or a LoRA adapter), with one or more storage locations.

**Authority.** A field on a deployment: `observed` (recorded and checked for drift, never written
to the cluster) or `managed` (the plane may change it). See
[ADR 0009](decisions/0009-authority-is-a-mark-not-a-consequence-of-adoption.md).

**Cache salt.** A per-tenant value the gateway adds to each text request (`nvext.cache_salt`) so
Dynamo's KV/prefix cache is never shared across tenants.

**CacheEntry.** Observed: whether an artifact's weights are resident on a cluster or node.

**Cluster.** A Kubernetes cluster enrolled in Hangar. Declared as a registry entry, with observed
state (reachability, sync state, operator version) reported by its operator.

**Component.** Something installed in a cluster and versioned independently: the operator, the
agent, the serving runtime, exporters.

**Console** (`libs/console`). The web UI. A client of the plane's HTTP API, like `hgr`.

**Control plane / plane** (`libs/plane`). The central service: the HTTP API, the hub and the
reactor, over one Postgres database. Stores intent; never in the inference request path.

**DCGM / DCGM exporter.** NVIDIA's GPU telemetry exporter. Hangar reads device identity (GPU UUIDs)
and health from it. See [ADR 0007](decisions/0007-device-identity-comes-from-the-dcgm-exporter.md).

**Declared / observed / operation.** The three kinds of object. Declared objects are written by the
plane and obeyed by the cluster; observed objects are written by the cluster and read by the plane;
operations are imperative, long-running and can fail (drain, scale, reboot).

**Deployment.** One workload on one cluster, carrying that cluster's share of the scale. The unit
the operator reconciles. Not a Kubernetes Deployment.

**Device.** One accelerator, identified by its UUID, with health, memory, MIG profile and current
allocation.

**Device class.** Hangar's name for a kind of accelerator (for example an 80 GB H100 SXM). Vendor
strings map to device classes through `libs/packages/contract/devices.json`.

**DGD** (DynamoGraphDeployment). NVIDIA Dynamo's custom resource describing a serving graph:
frontend, workers and their roles. The operator renders each Hangar deployment into one.

**Disaggregated serving.** Running prefill and decode as separate worker roles, each with its own
replica count and devices. A workload declares its roles; an aggregated workload has one.

**Dynamo** (NVIDIA Dynamo). The inference serving framework Hangar renders workloads into. It runs
vLLM, SGLang or TensorRT-LLM as the engine and provides the frontend, router and KV-aware routing.

**Endpoint.** The stable public model name clients call. Plane-only; resolves to one or more
deployments with weights or a failover order. The gateway reads endpoints, never workloads.

**Engine.** The inference runtime inside a worker: vLLM, SGLang or TensorRT-LLM.

**Enrolment token.** The per-cluster credential minted when a cluster is registered. The operator
presents it to the hub and the agent presents it to the gateway. It identifies exactly one cluster.

**Frontend.** Dynamo's OpenAI-compatible HTTP entry point for one model's deployment, which routes
to that deployment's workers. Hangar renders one per model
([ADR 0013](decisions/0013-one-frontend-per-model-routed-by-a-forwarded-deployment-id.md)).

**Gateway** (`libs/gateway`). The one Hangar process in the inference request path. Takes
OpenAI-compatible requests, picks a cluster from the routing table, and forwards over the tunnel.

**hgr** (`libs/cli`). The command line client for the plane's API.

**hgrbench** (`libs/benchmarking`). Runs several load generators against an OpenAI-compatible
endpoint and reports them side by side. A *ladder* is the list of concurrency levels a benchmark
steps through, and a *rung* is one level of it.

**hgrconform** (`libs/conformance`). Probes an OpenAI-compatible endpoint and compares its behaviour
with a reference provider.

**hgrtransit** (`libs/transit`). Measures the gateway, tunnel and agent path on one host, with no
GPU or cluster.

**Hub.** The plane process that terminates the operators' WebSocket streams, with resume and
backpressure.

**Internal / public listener.** The gateway's two client-facing listeners. The internal one has no
authentication and must not be reachable from outside your network; the public one identifies the
tenant from the `Host` header and can require an API key. See
[ADR 0016](decisions/0016-the-listener-is-the-guard-tenant-identity-replaces-the-shared-token.md).

**KV cache / prefix cache.** Attention state kept by the engine so repeated prompt prefixes are not
recomputed. Configured on the workload; unrelated to the weight cache.

**Metrics label.** The value of the `cluster` label a cluster's metrics carry in the central
metrics store, declared on the cluster's registry entry
([ADR 0011](decisions/0011-a-cluster-carries-its-own-metrics-label.md)).

**MIG** (Multi-Instance GPU). NVIDIA's partitioning of one GPU into isolated slices.

**Operator** (`hangar-operator`, `libs/operator`). The per-cluster process that receives desired
state from the hub, stores it in CRDs, renders it into Kubernetes and Dynamo objects, and reports
observed state back. Also a principal kind in the plane's auth.

**Placement.** The plane's choice of clusters for a workload and each cluster's share of its scale.
Picks clusters, never devices.

**Policy.** Placement constraints (regions, residency, device classes, allowed clusters) attached to
a tenant, endpoint, workload or cluster.

**Pool.** A declared provisioning spec for a homogeneous set of nodes in a cluster.

**PriorityClass.** A named priority for workloads. Carries a `preemptible` flag that is recorded but
not yet acted on (see the [roadmap](roadmap.md)).

**Project.** A unit inside a tenant that device quota attaches to.

**Quota.** A ceiling on how many devices of one class a project may hold, either in one cluster or
across the fleet; a cluster's own quota takes precedence over a fleet-wide one there. Checked by
placement at admission. It also records a guaranteed floor and an over-quota weight that nothing
acts on yet (see the [roadmap](roadmap.md)).

**Reactor.** The plane process (or loop inside the hub) that derives alerts and reconciles derived
configuration, under a lease.

**Replica.** Observed: one running instance of a deployment on named devices.

**`remote_write`.** The Prometheus protocol each cluster's metrics agent uses to push metrics to
the central store.

**Serving configuration.** The engine, version, quantization, parallelism, context length, cache
settings and engine arguments of a workload. Hashed into its `serving_digest`.

**ServingQuota.** A declared traffic commitment for one endpoint (request rate, token rate,
concurrency). Recorded and shown, not yet enforced (see the [roadmap](roadmap.md)).

**StreamCheckpoint.** The CRD in which the operator persists its resume cursor for the hub stream.

**SyncedResource.** The CRD in which the operator stores the desired state it received from the
hub, so a cluster keeps its intent across operator restarts and plane outages.

**TP / PP / EP.** Tensor, pipeline and expert parallelism: how a model is split across devices.

**Tenant.** An organisation using the installation's models. Owns endpoints, API keys and the
host name its requests arrive on. Also a principal kind in the plane's auth.

**TopologyDomain.** Observed: which devices share an NVLink/NVSwitch fabric, PCIe root or NUMA
node.

**Tunnel** (`libs/packages/tunnel`). The HTTP/2 connection the agent dials to the gateway, over
which the gateway sends requests into the cluster. See
[ADR 0018](decisions/0018-the-inference-tunnel-is-our-own-http2-role-reversal.md).

**Workload.** What you asked to run, globally: an artifact, its serving configuration, scale
bounds, priority and placement constraints. Has one deployment per cluster it runs on.
