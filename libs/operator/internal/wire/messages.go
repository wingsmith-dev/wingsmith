package wire

import (
	"encoding/json"
	"strings"
)

// ---------------------------------------------------------------------------- session

// Hello is the first frame the operator sends. It is never sent twice on one connection.
type Hello struct {
	ClusterID       string  `json:"cluster_id"`
	OperatorVersion string  `json:"operator_version"`
	Lossy           bool    `json:"lossy"`
	ResumeFrom      *uint64 `json:"resume_from,omitempty"`
	SessionID       *string `json:"session_id,omitempty"`
}

// Welcome is the hub's answer to Hello.
type Welcome struct {
	SessionID  string `json:"session_id"`
	AckSeq     uint64 `json:"ack_seq"`
	FullResync bool   `json:"full_resync"`
}

// Ping is an application-level liveness probe, sent by the operator every 10s.
type Ping struct{}

// Pong answers a Ping, correlated by ReplyTo.
type Pong struct{}

// Ack carries acknowledgement in the envelope's Ack field when there is nothing else to send.
// Its payload is empty by definition: the acknowledgement is the frame.
type Ack struct{}

// Error is the one generic failure frame.
type Error struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
}

// StreamEnd terminates a sequence of frames sharing one ReplyTo, and closes a resync.
//
// Complete is load-bearing: a receiver prunes local state against a snapshot only when the
// stream ended with Complete true. A truncated snapshot must leave local state untouched
// rather than be treated as an authoritative empty set.
type StreamEnd struct {
	Count    int  `json:"count"`
	Complete bool `json:"complete"`
}

// ---------------------------------------------------------------------------- resync

// ResyncBegin opens a full-resync stream. It is closed by StreamEnd; there is no ResyncEnd.
type ResyncBegin struct {
	Scope string `json:"scope"`
}

// ---------------------------------------------------------------------------- supply

// ClusterObserved is what the cluster reports about itself as a whole.
//
// Everything here is observed and none of it is declared. A cluster's registry entry says what we
// decided about it — its name, its region, whether it is draining. This says what it turned out to
// be: which cloud its machines are actually on and which Kubernetes distribution is actually
// running. The two disagreeing is a fact worth being able to see, which is exactly why the
// observed answer does not overwrite the declared one.
//
// Distribution is the one string on the wire that names an orchestrator. That is deliberate and
// bounded: it is an opaque label the plane stores and displays, never something it branches on.
// The moment the plane reads it to decide what to send, rule 2 is broken.
// ReadOnly is the one field here that is about the operator rather than the cluster, and it is
// the most consequential thing this frame can say. A read-only operator runs without the
// reconcile loop: it reports everything the cluster has and applies nothing the plane declares.
// Left off the wire, that is invisible from the plane — placement picks the cluster, the hub
// streams the desired state, the operator acknowledges the session and silently does nothing,
// and a workload sits at zero replicas with no error anywhere to explain it. Reporting it is
// what lets the console say that nothing will happen here, rather than leaving somebody to
// discover it by waiting.
//
// Multinode says a leader-worker backend — Grove or LeaderWorkerSet — is installed, so Dynamo
// can render a replica whose devices do not fit on one machine. Unlike ReadOnly and Distribution
// this one is not inert: placement must not send a replica that spans nodes to a cluster that
// cannot run one, so the plane reads it. It is discovered from the API groups the cluster serves
// rather than declared in the operator's config, because a flag asserting it is a second copy of
// a truth the operator can simply look up — and when the two disagree the cluster wins, silently,
// by hanging in collective initialisation.
type ClusterObserved struct {
	ClusterID    string `json:"cluster_id"`
	Distribution string `json:"distribution"`
	Cloud        string `json:"cloud"`
	Region       string `json:"region"`
	Nodes        int    `json:"nodes"`
	Devices      int    `json:"devices"`
	ReadOnly     bool   `json:"read_only"`
	Multinode    bool   `json:"multinode"`
	ObservedAt   string `json:"observed_at"`
}

// NodeCondition is one observed condition on a node. Cordon state is not a condition; it is the
// separate observed boolean NodeObserved.Cordoned.
type NodeCondition struct {
	Type    string `json:"type"`
	Status  string `json:"status"`
	Reason  string `json:"reason"`
	Message string `json:"message"`
	Since   string `json:"since"`
}

// NodeTaint is one scheduling repellent on a node, in the orchestrator-neutral form: a key, an
// optional value, and how hard it pushes back.
type NodeTaint struct {
	Key    string `json:"key"`
	Value  string `json:"value,omitempty"`
	Effect string `json:"effect"`
}

// NodeObserved is the full observed state of one node.
//
// InstanceType, CPUCores, MemoryMiB, CPUArch and OSImage are what the machine *is*, as opposed to
// how it is currently doing. They are stated in neutral units — cores and mebibytes — rather than
// forwarded as the orchestrator's own quantity strings, because the plane does not know a cluster
// is Kubernetes and must not learn to parse its resource grammar to read a number.
//
// Cloud is the provider the machine belongs to, taken from the node's provider reference. It is
// observed per node rather than declared per cluster: a cluster can be told it is in one place and
// be wrong, and a hybrid cluster would make the cluster-level answer a lie for some of its nodes.
type NodeObserved struct {
	NodeID          string            `json:"node_id"`
	ClusterID       string            `json:"cluster_id"`
	Name            string            `json:"name"`
	Pool            string            `json:"pool"`
	Membership      string            `json:"membership"`
	Ready           bool              `json:"ready"`
	Cordoned        bool              `json:"cordoned"`
	Conditions      []NodeCondition   `json:"conditions"`
	InstanceType    string            `json:"instance_type"`
	Cloud           string            `json:"cloud"`
	Zone            string            `json:"zone"`
	Region          string            `json:"region"`
	CPUCores        int               `json:"cpu_cores"`
	MemoryMiB       int64             `json:"memory_mib"`
	CPUArch         string            `json:"cpu_arch"`
	OSImage         string            `json:"os_image"`
	KernelVersion   string            `json:"kernel_version"`
	DriverVersion   string            `json:"driver_version"`
	RuntimeVersion  string            `json:"runtime_version"`
	OperatorVersion string            `json:"operator_version"`
	Taints          []NodeTaint       `json:"taints"`
	DeviceIDs       []string          `json:"device_ids"`
	Labels          map[string]string `json:"labels"`
	ObservedAt      string            `json:"observed_at"`
}

// NodeEvent reports something that happened to a node at a point in time.
type NodeEvent struct {
	NodeID     string `json:"node_id"`
	ClusterID  string `json:"cluster_id"`
	Event      string `json:"event"`
	Reason     string `json:"reason"`
	Message    string `json:"message"`
	OccurredAt string `json:"occurred_at"`
}

// DeviceEcc is a device's ECC configuration and error counters.
type DeviceEcc struct {
	ModeEnabled          bool `json:"mode_enabled"`
	VolatileCorrected    int  `json:"volatile_corrected"`
	VolatileUncorrected  int  `json:"volatile_uncorrected"`
	AggregateUncorrected int  `json:"aggregate_uncorrected"`
}

// DeviceMig is a device's MIG partitioning state.
type DeviceMig struct {
	Enabled    bool    `json:"enabled"`
	Profile    *string `json:"profile,omitempty"`
	InstanceID *string `json:"instance_id,omitempty"`
}

// DeviceAllocation is what a device is currently committed to.
type DeviceAllocation struct {
	ReplicaID    string `json:"replica_id"`
	DeploymentID string `json:"deployment_id"`
	ProjectID    string `json:"project_id"`
}

// DeviceObserved is the full observed state of one accelerator. Utilization, temperature,
// throughput and memory in use are deliberately absent; see PROTOCOL §7. MemoryTotalMB is
// capacity, which is state; how much of it is occupied is a measurement and goes elsewhere.
type DeviceObserved struct {
	DeviceID         string            `json:"device_id"`
	NodeID           string            `json:"node_id"`
	ClusterID        string            `json:"cluster_id"`
	UUID             string            `json:"uuid"`
	Index            int               `json:"index"`
	Model            string            `json:"model"`
	DeviceClass      string            `json:"device_class"`
	MemoryTotalMB    int64             `json:"memory_total_mb"`
	Health           string            `json:"health"`
	Ecc              DeviceEcc         `json:"ecc"`
	Throttled        bool              `json:"throttled"`
	ThrottleReasons  []string          `json:"throttle_reasons"`
	Mig              DeviceMig         `json:"mig"`
	TopologyDomainID *string           `json:"topology_domain_id,omitempty"`
	Allocation       *DeviceAllocation `json:"allocation,omitempty"`
	ObservedAt       string            `json:"observed_at"`
}

// DeviceEvent reports something that happened to a device at a point in time.
type DeviceEvent struct {
	DeviceID   string `json:"device_id"`
	NodeID     string `json:"node_id"`
	ClusterID  string `json:"cluster_id"`
	Event      string `json:"event"`
	Reason     string `json:"reason"`
	Message    string `json:"message"`
	OccurredAt string `json:"occurred_at"`
}

// TopologyObserved is one topology domain, the devices that share it, and the nodes it spans.
//
// NodeIDs is a set because a domain is not a property of a machine: an NVSwitch fabric or a
// GB200 rack is one NVLink domain across many nodes. Zero, one, or many are all legal, and
// SpansNodes is the cluster's own answer rather than something the plane recomputes.
type TopologyObserved struct {
	DomainID   string   `json:"domain_id"`
	ClusterID  string   `json:"cluster_id"`
	Kind       string   `json:"kind"`
	NodeIDs    []string `json:"node_ids"`
	SpansNodes bool     `json:"spans_nodes"`
	DeviceIDs  []string `json:"device_ids"`
	ObservedAt string   `json:"observed_at"`
}

// CapacityClass is the capacity rollup for one device class within a pool.
type CapacityClass struct {
	DeviceClass string `json:"device_class"`
	Total       int    `json:"total"`
	Free        int    `json:"free"`
	Allocated   int    `json:"allocated"`
	Unhealthy   int    `json:"unhealthy"`
}

// CapacityObserved is the derived capacity rollup for one pool.
type CapacityObserved struct {
	ClusterID  string          `json:"cluster_id"`
	Pool       string          `json:"pool"`
	Classes    []CapacityClass `json:"classes"`
	ObservedAt string          `json:"observed_at"`
}

// ---------------------------------------------------------------------------- demand, hub -> op

// Overlay is the escape hatch for what this plane does not model.
//
// Engines and their operators carry far more configuration than a control plane wants to mirror
// field by field, and mirroring it is a losing race. The serving configuration models what the
// plane reasons about and passes the rest through.
type Overlay struct {
	// PodSpec is merged over the rendered pod with strategic-merge semantics, so containers and
	// env merge by name. The renderer refuses any key outside its allowlist (render/overlay.go),
	// which mirrors the plane's.
	PodSpec map[string]any `json:"pod_spec"`

	// Dynamo is merged over the rendered graph deployment: maps merge and lists replace. Only
	// annotations and envs are accepted.
	Dynamo map[string]any `json:"dynamo"`
}

// Parallelism is how one replica is split across its devices.
type Parallelism struct {
	Tensor   int `json:"tensor"`
	Pipeline int `json:"pipeline"`
	Expert   int `json:"expert"`
}

// Resources is the non-accelerator footprint of one replica.
type Resources struct {
	CPUCores  *int   `json:"cpu_cores"`
	MemoryMiB *int64 `json:"memory_mib"`
	DiskGiB   *int   `json:"disk_gib,omitempty"`
}

// ServingSpec is how to serve an artifact, in enough detail for a cluster to render it.
//
// It travels inlined on the deployment rather than as a declared object of its own. The separate
// object it replaces was filtered and serialised per cluster anyway, so sharing one between two
// deployments saved nothing on the wire and bought an ordering rule, a pending reason, and a
// resume race in which a deployment waited forever on a configuration it was never sent.
//
// Digest is the identity: two deployments that would produce the same running process carry the
// same digest, which is what reuse means now that nothing is shared by reference.
//
// The device shape is deliberately absent — it arrives on the deployment's Placement, derived from
// this configuration, and carrying it twice invites the two copies to disagree.
type ServingSpec struct {
	// Engine is which engine the worker launches inside the runtime: a module name and a flag
	// dialect, and no version at all.
	Engine string `json:"engine"`
	// EngineRuntimeVersion is a tag of the Dynamo runtime the engine runs in -- 1.4.1 -- and is
	// never the engine's own version. The vLLM inside vllm-runtime:1.4.1 is on 0.28.0; the two
	// count in different sequences, and which build NVIDIA compiled in is not on this wire.
	EngineRuntimeVersion string `json:"engine_runtime_version"`
	// EngineRuntimeImage is the whole address when the plane pins one instead of deriving it.
	EngineRuntimeImage *string `json:"engine_runtime_image"`

	// FrontendImage is the runtime the OpenAI-compatible frontend for this model runs, when it
	// must differ from the engine's.
	//
	// A field of its own rather than a reuse of EngineRuntimeImage, because the two can be
	// genuinely different builds: a model can need a patched frontend while its workers stay on
	// the stock engine image, when the defect is in the frontend binary and not in the engine.
	// Nil is the ordinary case and means the
	// cluster's own default, which is why this is a pointer — an empty string from a plane too
	// old to send the field must not read as a deliberate blank image.
	FrontendImage      *string           `json:"frontend_image"`
	EngineArgs         map[string]string `json:"engine_args"`
	Quantization       *string           `json:"quantization"`
	Dtype              *string           `json:"dtype"`
	Parallelism        Parallelism       `json:"parallelism"`
	ContextLength      int               `json:"context_length"`
	MaxBatchTokens     *int              `json:"max_batch_tokens"`
	KVCacheDtype       *string           `json:"kv_cache_dtype"`
	KVCacheFraction    *float64          `json:"kv_cache_fraction"`
	PrefixCacheEnabled bool              `json:"prefix_cache_enabled"`
	PrefixCacheConfig  map[string]string `json:"prefix_cache_config"`
	Resources          Resources         `json:"resources"`
	// Digest is `svg_` and a sha256 over the fields above, computed by the plane and never
	// derived here. It names the footprint and nothing else: what a model is called, what it
	// was imported with and what it asserts are all outside it, because the question it answers
	// is whether two deployments would produce the same running process.
	Digest  string  `json:"digest"`
	Overlay Overlay `json:"overlay"`

	// Topology is `aggregated` or `disaggregated`, and empty from a plane too old to send it.
	//
	// Empty reads as aggregated, which is what `Disaggregated` is for. That is not politeness
	// about old planes: it is the same guarantee the digest keeps, because an aggregated
	// deployment must render byte-identically to what it rendered before this field existed.
	Topology string `json:"topology,omitempty"`

	// Roles is the per-role configuration of a disaggregated deployment, and it carries no
	// counts.
	//
	// The split between this and `DeploymentDesired.RoleReplicas` is the digest split made
	// structural. What is here decides what the process *is* — prefill runs
	// `--max-num-batched-tokens` 32768 against decode's 2048 — and is therefore inside
	// `Digest` above. How many of each there are is scale, it is in the deployment's frame
	// beside `MinReplicas`, and it is deliberately not hashed: `ops_benchmark_run.serving_digest`
	// is what links a measurement to the configuration it measured, and a count in that hash
	// would orphan every benchmark on a model the moment somebody added a prefill worker.
	//
	// Empty on an aggregated deployment, and empty is the only way to say that — a single-entry
	// `roles` meaning "one worker" would make every existing deployment a disaggregated one with
	// a degenerate ratio, and the render has to stay byte-identical for those.
	Roles []RoleSpec `json:"roles,omitempty"`

	// Router is how this deployment's own frontend routes between its workers, and nil from a
	// plane too old to send it or for a workload that declares nothing.
	//
	// Nil and a block with every field nil are the same statement, and both render exactly what
	// the frontend rendered before this field existed: KV routing and nothing else. That is the
	// byte-identity bar `Topology` keeps, for the same reason — Dynamo hashes the frontend's pod
	// template, so an extra variable is a rolled frontend on every deployment in the fleet.
	Router *RouterSpec `json:"router,omitempty"`

	// OutputModality is what the engine emits: `image` or `video` for a diffusion workload, and
	// empty for everything else, which means text.
	//
	// Empty rather than `text` on a chat deployment so that its frame is byte-identical to what it
	// was before this field existed. It renders as `--output-modalities` on the omni engine and is
	// refused by the render on any other, because `dynamo.vllm` has no such flag.
	OutputModality string `json:"output_modality,omitempty"`

	// Diffusion is the per-deployment pipeline configuration of an omni worker, nil when none is
	// declared.
	Diffusion *DiffusionSpec `json:"diffusion,omitempty"`
}

// DiffusionSpec is what vLLM-Omni reads from a stage-config file and not from its command line.
//
// Typed rather than a raw YAML blob, for the reason RouterSpec is: the operator writes the file
// and owns its fixed parts (one stage, the devices the pod holds, the executor), and the plane
// declares only the two things that vary per model. The Dynamo omni CLI refuses
// `--model-class-name`, so the class has no other way in, and omni's default of 50 inference
// steps is wrong for every distilled model we serve.
type DiffusionSpec struct {
	// ModelClassName is the pipeline class, e.g. `LTX2Pipeline`, or empty to let omni infer it.
	ModelClassName string `json:"model_class_name,omitempty"`
	// DefaultSamplingParams are the stage's defaults, e.g. `num_inference_steps` and
	// `guidance_scale`. A request's own values still win.
	DefaultSamplingParams map[string]any `json:"default_sampling_params,omitempty"`
}

// RouterSpec is the frontend's router configuration, one field per Dynamo setting it renders.
//
// Typed and closed rather than an environment passthrough. Each field names exactly one
// `DYN_ROUTER_*` variable, the plane validates every value against what Dynamo accepts before it
// is sent, and a nil field renders no variable at all, so Dynamo's own default stands.
type RouterSpec struct {
	// Mode is `DYN_ROUTER_MODE`. Nil keeps the operator's own default, `kv`.
	Mode *string `json:"mode,omitempty"`
	// QueueThreshold is `DYN_ROUTER_QUEUE_THRESHOLD`: a fraction of a worker's batched-token
	// budget above which requests wait in the router. Nil means the router does not queue.
	QueueThreshold *float64 `json:"queue_threshold,omitempty"`
	// QueuePolicy is `DYN_ROUTER_QUEUE_POLICY`, `fcfs` or `wspt`, and means nothing without a
	// QueueThreshold.
	QueuePolicy *string `json:"queue_policy,omitempty"`
	// KVOverlapScoreCredit is `DYN_ROUTER_KV_OVERLAP_SCORE_CREDIT`: how much a cached prefix on a
	// worker discounts its prefill load when the KV router picks one.
	KVOverlapScoreCredit *float64 `json:"kv_overlap_score_credit,omitempty"`
}

// RoleSpec is one role of a disaggregated deployment: what makes its process different from the
// other role's, and nothing about how many of it run.
//
// Narrow because the difference is narrow. Across NVIDIA's published recipe the two roles differ
// on six arguments and agree on the artifact, the tensor degree, the device class, the context
// length, the quantization and the `kv-cache-dtype` — and on `kv-transfer-config`, which is
// literally the same string on both sides because the transfer mechanism belongs to the topology
// rather than to the role. So a role is one typed number and an argument map.
type RoleSpec struct {
	// Role is `prefill` or `decode`. A closed set, validated by the plane before it is sent,
	// because a role outside it renders a `--disaggregation-mode` no engine accepts — a worker
	// that will not start rather than a validation error somebody can read.
	Role string `json:"role"`
	// MaxBatchTokens is this role's `--max-num-batched-tokens`, or nil to inherit the
	// deployment's. Typed rather than left to EngineArgs because the serving spec already types
	// it and the operator already derives the flag from it.
	MaxBatchTokens *int `json:"max_batch_tokens,omitempty"`
	// EngineArgs is laid over the deployment's own, and may not name a flag the operator
	// derives — including `disaggregation-mode` and `kv-transfer-config`, which this topology
	// now decides. The plane refuses those at declaration; this only carries what survived.
	EngineArgs map[string]string `json:"engine_args,omitempty"`
}

// RuntimeVersion is the Dynamo runtime tag this deployment asks for. Empty means the plane pinned
// an image instead, which RuntimeImage answers.
func (s ServingSpec) RuntimeVersion() string {
	return strings.TrimSpace(s.EngineRuntimeVersion)
}

// RuntimeImage is the image the engine worker runs, or nil when the plane declared none.
func (s ServingSpec) RuntimeImage() *string {
	if s.EngineRuntimeImage == nil || strings.TrimSpace(*s.EngineRuntimeImage) == "" {
		return nil
	}
	return s.EngineRuntimeImage
}

// PlacementConstraints is the device shape and placement rules a deployment binds against.
type PlacementConstraints struct {
	DeviceClass       string            `json:"device_class"`
	DevicesPerReplica int               `json:"devices_per_replica"`
	MinDeviceMemoryMB int64             `json:"min_device_memory_mb"`
	Topology          string            `json:"topology"`
	AllowedPools      []string          `json:"allowed_pools"`
	NodeSelector      map[string]string `json:"node_selector"`
}

// DeploymentDesired is the declared state of one deployment. Full object, never a patch.
// WorkloadDiscovered is a workload found already running in the cluster that the plane never
// declared. It is a sighting and nothing more: there is no declared object behind it, so there is
// nothing for a reconciler to converge and no reason for anything to restart.
type WorkloadDiscovered struct {
	WorkloadID        string            `json:"workload_id"`
	ClusterID         string            `json:"cluster_id"`
	Namespace         string            `json:"namespace"`
	Name              string            `json:"name"`
	Kind              string            `json:"kind"`
	Images            []string          `json:"images"`
	ReplicasDesired   int               `json:"replicas_desired"`
	ReplicasReady     int               `json:"replicas_ready"`
	DevicesPerReplica int               `json:"devices_per_replica"`
	DevicesTotal      int               `json:"devices_total"`
	NodeIDs           []string          `json:"node_ids"`
	Labels            map[string]string `json:"labels"`
	Model             string            `json:"model,omitempty"`
	SpecFingerprint   string            `json:"spec_fingerprint"`
	Spec              *WorkloadSpec     `json:"spec,omitempty"`
	ObservedAt        string            `json:"observed_at"`
}

// WorkloadSpec is how a discovered workload was told to serve: the image it runs, the arguments
// it was given, and the resources it asked for. Verbatim and uninterpreted — the plane reads
// `--tensor-parallel-size` as parallelism, this only carries it.
//
// `env_names` is names without values, and that is a guarantee rather than an omission. Serving
// pods are routinely handed registry credentials and HuggingFace tokens through their
// environment; shipping values would copy them into the plane's database and into its backups.
type WorkloadSpec struct {
	Image        string            `json:"image,omitempty"`
	Command      []string          `json:"command,omitempty"`
	Args         []string          `json:"args,omitempty"`
	EnvNames     []string          `json:"env_names,omitempty"`
	Requests     map[string]string `json:"requests,omitempty"`
	Limits       map[string]string `json:"limits,omitempty"`
	NodeSelector map[string]string `json:"node_selector,omitempty"`
}

// Adopt rides on a DeploymentDesired that was promoted from a discovered workload. Its presence
// changes the frame's meaning entirely: the named object already exists and is already serving, so
// the operator takes ownership of it in place rather than applying a rendered spec over it.
//
// SpecFingerprint is what was true when the promotion was accepted. A reconciler that finds the
// live object digesting to anything else must refuse and say so — the workload drifted, or the
// render disagrees with reality, and either way converging means restarting a model to close a gap
// nobody asked to close.
type Adopt struct {
	Namespace       string `json:"namespace"`
	Kind            string `json:"kind"`
	Name            string `json:"name"`
	SpecFingerprint string `json:"spec_fingerprint"`
}

type DeploymentDesired struct {
	DeploymentID  string `json:"deployment_id"`
	Name          string `json:"name,omitempty"`
	ClusterID     string `json:"cluster_id"`
	ProjectID     string `json:"project_id"`
	Lifecycle     string `json:"lifecycle"`
	PriorityClass string `json:"priority_class"`
	// MinReplicas is this cluster's share of the workload's total scale, never the total, and
	// it is the number that is actually rendered. MaxReplicas is reserved for an autoscaler on
	// the roadmap; nothing reads it today. Neither may be summed with, or derived from, anything
	// outside this frame.
	MinReplicas int `json:"min_replicas"`
	MaxReplicas int `json:"max_replicas"`
	// RoleReplicas is the absolute worker count for each role of a disaggregated deployment,
	// already multiplied by the plane. Empty on an aggregated one, where `MinReplicas` is the
	// whole answer.
	//
	// Absolute, and that is the point of the field. `MinReplicas` under a disaggregated topology
	// counts *groups*, and a group is the per-role weights taken together — so the per-role
	// count is `MinReplicas x weight`. The multiplication happens in the plane and never here,
	// because `Replicas` below states its own contract: it "renders exactly the number it is
	// sent, and must never sum, scale, or re-derive it against anything else". An operator doing
	// arithmetic on a replica count would be the first exception to an invariant that is
	// currently absolute and cheap to check, and the alternative costs one map on a frame the
	// plane already sends.
	//
	// Keyed by the same role names as `Serving.Roles`, and carried here rather than inside them
	// because this is scale and those are configuration. See `RoleSpec`.
	RoleReplicas map[string]int       `json:"role_replicas,omitempty"`
	Placement    PlacementConstraints `json:"placement"`
	// Serving is how to run this deployment's model, inlined rather than referenced. It is
	// never absent and never late: it arrives in the same frame as the deployment that needs
	// it, so there is no ordering between the two and nothing to wait for.
	Serving     ServingSpec `json:"serving"`
	ArtifactIDs []string    `json:"artifact_ids"`
	// ServedModelName is the model id an API client sends, already resolved by the plane: the
	// workload's own declaration, or the artifact's lowercase Hugging Face slug when it declares
	// none. Never empty, and never derived here — the plane owns the order so that one function
	// answers "what is this model called" for the console, the gateway and the engine alike.
	// Serving.EngineArgs still wins over it, because engine_args is laid over the derived flags.
	ServedModelName string `json:"served_model_name"`
	Generation      int64  `json:"generation"`
	UpdatedAt       string `json:"updated_at"`
	Authority       string `json:"authority,omitempty"`
	Adopt           *Adopt `json:"adopt,omitempty"`
}

// DeploymentDeleted withdraws a deployment.
type DeploymentDeleted struct {
	DeploymentID string `json:"deployment_id"`
	ClusterID    string `json:"cluster_id"`
	Generation   int64  `json:"generation"`
	DeletedAt    string `json:"deleted_at"`
}

// ArtifactSource is one place an artifact can be fetched from.
type ArtifactSource struct {
	Kind          string  `json:"kind"`
	URI           string  `json:"uri"`
	Priority      int     `json:"priority"`
	CredentialRef *string `json:"credential_ref,omitempty"`
}

// ArtifactDesired declares an immutable, content-addressed artifact and where it comes from.
type ArtifactDesired struct {
	ArtifactID string           `json:"artifact_id"`
	ClusterID  string           `json:"cluster_id"`
	Kind       string           `json:"kind"`
	Digest     string           `json:"digest"`
	Format     string           `json:"format"`
	SizeBytes  int64            `json:"size_bytes"`
	Licence    string           `json:"licence"`
	Sources    []ArtifactSource `json:"sources"`
	UpdatedAt  string           `json:"updated_at"`
}

// ComponentDesired declares what is installed in a cluster and at what version.
type ComponentDesired struct {
	ComponentID string            `json:"component_id"`
	ClusterID   string            `json:"cluster_id"`
	Name        string            `json:"name"`
	Version     string            `json:"version"`
	Enabled     bool              `json:"enabled"`
	Config      map[string]string `json:"config"`
	UpdatedAt   string            `json:"updated_at"`
}

// CacheWarmEntry is one artifact in a cluster's declared warm-set.
type CacheWarmEntry struct {
	ArtifactID string `json:"artifact_id"`
	Priority   int    `json:"priority"`
	Pin        bool   `json:"pin"`
}

// CacheDesired is the weight-cache warm-set for one cluster. Unrelated to KV cache, which is
// serving configuration.
type CacheDesired struct {
	ClusterID      string           `json:"cluster_id"`
	Entries        []CacheWarmEntry `json:"entries"`
	EvictionPolicy string           `json:"eviction_policy"`
	CapacityBytes  int64            `json:"capacity_bytes"`
	UpdatedAt      string           `json:"updated_at"`
}

// ---------------------------------------------------------------------------- demand, op -> hub

// LastError is the most recent failure attached to an observed object.
type LastError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	At      string `json:"at"`
}

// RoleObserved is one role's own share of a deployment's rollout.
//
// Desired is what this cluster rendered for the role, which is `role_replicas` echoed back, and
// not a number this operator derived. See DeploymentObserved.Roles for why the scalars beside it
// are still the whole answer for anyone who does not read this.
type RoleObserved struct {
	Role    string `json:"role"`
	Desired int    `json:"desired"`
	Ready   int    `json:"ready"`
}

// DeploymentObserved is the observed state of one deployment.
type DeploymentObserved struct {
	DeploymentID        string     `json:"deployment_id"`
	ClusterID           string     `json:"cluster_id"`
	Generation          int64      `json:"generation"`
	Phase               string     `json:"phase"`
	DesiredReplicas     int        `json:"desired_replicas"`
	ReadyReplicas       int        `json:"ready_replicas"`
	UpdatedReplicas     int        `json:"updated_replicas"`
	UnavailableReplicas int        `json:"unavailable_replicas"`
	LastError           *LastError `json:"last_error,omitempty"`
	ObservedAt          string     `json:"observed_at"`

	// ReplicaIDs is every replica this deployment has on this cluster right now, and it is
	// what lets the plane forget the ones it no longer has. Demand state is never pruned
	// against a resync, so a replica that simply stopped being reported — a rolled pod, a
	// restarted one, a node that moved — stayed in the plane's database for ever and kept
	// counting towards the rollup, which is why a deployment of one read three ready of one.
	//
	// Nil and empty mean different things and the field is deliberately not `omitempty`: nil
	// is a frame from an operator that predates this and makes no claim, empty is a claim
	// that there are none. A scale to zero has to be able to say so.
	ReplicaIDs []string `json:"replica_ids"`

	// Reason is the cluster's sub-state within Phase, from the closed set in MESSAGES.md.
	// It is not an error: most of what it reports is an ordering that has not been satisfied
	// yet, which is the normal way a deployment spends its first minutes.
	Reason string `json:"reason,omitempty"`

	// Message is the same fact written for a person, and is never parsed.
	Message string `json:"message,omitempty"`

	// Roles is the same rollout split by role, and it is added beside the four scalars above
	// rather than instead of them.
	//
	// The scalars stay the group rollup, and that is the compatibility story rather than a
	// concession to it: a plane too old to read this field sums the roles the same way this
	// operator already did and is *right, less precisely*. Nothing it can conclude from
	// `3 of 3 ready` becomes false; it simply cannot see which of the three is the one missing
	// when it is `2 of 3`, which for a 1P:2D group is the whole question — two ready decode
	// workers and no prefill serve nothing, and the totals cannot say so.
	//
	// Omitted entirely on an aggregated deployment. An empty array would be a claim that this
	// deployment has no roles, where a single-entry one would make every deployment in the fleet
	// a disaggregated one with a degenerate ratio; absent is the only spelling that says the
	// question does not apply.
	Roles []RoleObserved `json:"roles,omitempty"`
}

// ReplicaObserved is one running instance on named devices.
type ReplicaObserved struct {
	ReplicaID            string   `json:"replica_id"`
	Name                 string   `json:"name"`
	DeploymentID         string   `json:"deployment_id"`
	ClusterID            string   `json:"cluster_id"`
	NodeID               string   `json:"node_id"`
	DeviceIDs            []string `json:"device_ids"`
	Phase                string   `json:"phase"`
	Ready                bool     `json:"ready"`
	Engine               string   `json:"engine"`
	EngineRuntimeVersion string   `json:"engine_runtime_version"`
	ServingDigest        string   `json:"serving_digest"`
	RestartCount         int      `json:"restart_count"`
	ObservedGeneration   int64    `json:"observed_generation"`
	StartedAt            *string  `json:"started_at,omitempty"`
	ObservedAt           string   `json:"observed_at"`
	// Role is `prefill` or `decode` for a replica of a disaggregated deployment, and empty for
	// an aggregated one.
	//
	// Read off the pod label the renderer stamps, never inferred from the pod's name: the name
	// is Dynamo's convention and could change under us, where the label is a string this
	// operator wrote and both sides agree on.
	//
	// Omitted when empty so an aggregated deployment's replica frames stay byte-identical to
	// what they were, which matters to the fixture corpus rather than to any cluster.
	Role string `json:"role,omitempty"`
}

// ReplicaEvent reports something that happened to a replica at a point in time.
type ReplicaEvent struct {
	ReplicaID    string `json:"replica_id"`
	DeploymentID string `json:"deployment_id"`
	ClusterID    string `json:"cluster_id"`
	Event        string `json:"event"`
	Reason       string `json:"reason"`
	Message      string `json:"message"`
	OccurredAt   string `json:"occurred_at"`
}

// AllocationObserved is the set of devices currently committed to a replica.
type AllocationObserved struct {
	AllocationID string   `json:"allocation_id"`
	ClusterID    string   `json:"cluster_id"`
	ReplicaID    string   `json:"replica_id"`
	DeploymentID string   `json:"deployment_id"`
	ProjectID    string   `json:"project_id"`
	NodeID       string   `json:"node_id"`
	DeviceIDs    []string `json:"device_ids"`
	Exclusive    bool     `json:"exclusive"`
	GrantedAt    string   `json:"granted_at"`
	ObservedAt   string   `json:"observed_at"`
}

// CacheNodeResidency is what one node holds of one artifact. A node mid-fetch and a node
// already serving are not the same fact, so state and bytes are per node rather than rolled up.
type CacheNodeResidency struct {
	NodeID        string  `json:"node_id"`
	State         string  `json:"state"`
	BytesResident int64   `json:"bytes_resident"`
	Path          *string `json:"path,omitempty"`
}

// CacheObserved is the residency of one artifact against the declared warm-set.
type CacheObserved struct {
	ClusterID        string               `json:"cluster_id"`
	ArtifactID       string               `json:"artifact_id"`
	State            string               `json:"state"`
	Resident         bool                 `json:"resident"`
	BytesResident    int64                `json:"bytes_resident"`
	Nodes            []CacheNodeResidency `json:"nodes"`
	EvictionPressure float64              `json:"eviction_pressure"`
	LastAccessedAt   *string              `json:"last_accessed_at,omitempty"`
	ObservedAt       string               `json:"observed_at"`
	// Reason and Message say why this state and not another. Free text, omitted when empty, so
	// a plane that predates them is unaffected: `absent` alone cannot distinguish a download
	// that has not started from one nothing on this cluster is able to start.
	Reason  string `json:"reason,omitempty"`
	Message string `json:"message,omitempty"`
}

// ComponentObserved is the rollout status of one component.
type ComponentObserved struct {
	ComponentID    string     `json:"component_id"`
	ClusterID      string     `json:"cluster_id"`
	Name           string     `json:"name"`
	DesiredVersion string     `json:"desired_version"`
	RunningVersion string     `json:"running_version"`
	Phase          string     `json:"phase"`
	ReadyInstances int        `json:"ready_instances"`
	TotalInstances int        `json:"total_instances"`
	LastError      *LastError `json:"last_error,omitempty"`
	ObservedAt     string     `json:"observed_at"`
}

// ---------------------------------------------------------------------------- operations

// OperationTarget is what an operation acts upon.
type OperationTarget struct {
	Kind string `json:"kind"`
	ID   string `json:"id"`
}

// OperationDesired is an imperative, long-running, failable instruction.
type OperationDesired struct {
	OperationID string            `json:"operation_id"`
	ClusterID   string            `json:"cluster_id"`
	Kind        string            `json:"kind"`
	Target      OperationTarget   `json:"target"`
	Reason      string            `json:"reason"`
	RequestedBy string            `json:"requested_by"`
	Deadline    string            `json:"deadline"`
	Parameters  map[string]string `json:"parameters"`
	CreatedAt   string            `json:"created_at"`
}

// OperationFailure is why an operation failed.
type OperationFailure struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
}

// OperationObserved is the progress and outcome of an operation.
type OperationObserved struct {
	OperationID string            `json:"operation_id"`
	ClusterID   string            `json:"cluster_id"`
	Phase       string            `json:"phase"`
	Progress    float64           `json:"progress"`
	Message     string            `json:"message"`
	Result      map[string]string `json:"result,omitempty"`
	Failure     *OperationFailure `json:"failure,omitempty"`
	StartedAt   *string           `json:"started_at,omitempty"`
	CompletedAt *string           `json:"completed_at,omitempty"`
	ObservedAt  string            `json:"observed_at"`
}

// OperationCancel withdraws an operation the plane has already sent.
//
// A frame of its own rather than a field on OperationDesired, because a cancellation is not a
// restatement of the instruction: the operation is already accepted, probably already running, and
// what is being sent is a second imperative about the first. Re-sending the desired frame with a
// flag on it would also make cancellation prunable by a resync, which is exactly the property
// operations are kept out of the snapshot to avoid.
//
// The receiver answers by reporting a terminal phase for OperationID, the same as it would for an
// operation that finished on its own. An id this operator does not hold is not an error — the
// operation may have finished a moment before the frame arrived, or been refused, or belonged to a
// process that has since restarted — so a cancellation is at-least-once and idempotent.
type OperationCancel struct {
	OperationID string `json:"operation_id"`
	ClusterID   string `json:"cluster_id"`
	Reason      string `json:"reason"`
	RequestedBy string `json:"requested_by"`
	RequestedAt string `json:"requested_at"`
}

// BenchmarkObserved is the outcome of a benchmark run.
//
// Beside OperationObserved rather than inside it: that message's Result is a map[string]string and
// every load generator worth running emits nested JSON. Widening it would widen it for drain and
// cordon too, which have nothing to put there.
//
// Summary is json.RawMessage and not a struct. It is the harness's own export, forwarded byte for
// byte, because normalising AIPerf's shape and vLLM's into shared fields is a decision that needs
// both formats read against a real deployment first. HarnessVersion is what makes two runs
// comparable in the meantime.
type BenchmarkObserved struct {
	BenchmarkID    string          `json:"benchmark_id"`
	ClusterID      string          `json:"cluster_id"`
	DeploymentID   string          `json:"deployment_id"`
	Harness        string          `json:"harness"`
	Phase          string          `json:"phase"`
	ObservedAt     string          `json:"observed_at"`
	HarnessVersion string          `json:"harness_version,omitempty"`
	Summary        json.RawMessage `json:"summary,omitempty"`
	ArtifactsURI   string          `json:"artifacts_uri,omitempty"`
	// LogTail is the last few KiB of the harness's stdout, tail-biased because that is where
	// the failure lives. A benchmark that wants its full log preserved should publish it to
	// `ArtifactsURI`; this channel exists so a failed run and a truncated summary can both be
	// reconstructed without shell access to a cluster somebody else owns. Capped, never the
	// whole thing.
	LogTail     string  `json:"log_tail,omitempty"`
	Error       string  `json:"error,omitempty"`
	StartedAt   *string `json:"started_at,omitempty"`
	CompletedAt *string `json:"completed_at,omitempty"`
}

// MessageTypes maps every wire type to a constructor for its payload. A type absent here is
// unknown to this operator and its frames are logged and dropped.
var MessageTypes = map[string]func() any{
	"hello":               func() any { return &Hello{} },
	"welcome":             func() any { return &Welcome{} },
	"ping":                func() any { return &Ping{} },
	"pong":                func() any { return &Pong{} },
	"error":               func() any { return &Error{} },
	"ack":                 func() any { return &Ack{} },
	"stream.end":          func() any { return &StreamEnd{} },
	"resync.begin":        func() any { return &ResyncBegin{} },
	"cluster.observed":    func() any { return &ClusterObserved{} },
	"node.observed":       func() any { return &NodeObserved{} },
	"node.event":          func() any { return &NodeEvent{} },
	"device.observed":     func() any { return &DeviceObserved{} },
	"device.event":        func() any { return &DeviceEvent{} },
	"topology.observed":   func() any { return &TopologyObserved{} },
	"capacity.observed":   func() any { return &CapacityObserved{} },
	"workload.discovered": func() any { return &WorkloadDiscovered{} },
	"deployment.desired":  func() any { return &DeploymentDesired{} },
	"deployment.deleted":  func() any { return &DeploymentDeleted{} },
	"artifact.desired":    func() any { return &ArtifactDesired{} },
	"component.desired":   func() any { return &ComponentDesired{} },
	"cache.desired":       func() any { return &CacheDesired{} },
	"deployment.observed": func() any { return &DeploymentObserved{} },
	"replica.observed":    func() any { return &ReplicaObserved{} },
	"replica.event":       func() any { return &ReplicaEvent{} },
	"allocation.observed": func() any { return &AllocationObserved{} },
	"cache.observed":      func() any { return &CacheObserved{} },
	"component.observed":  func() any { return &ComponentObserved{} },
	"operation.desired":   func() any { return &OperationDesired{} },
	"operation.observed":  func() any { return &OperationObserved{} },
	"operation.cancel":    func() any { return &OperationCancel{} },
	"benchmark.observed":  func() any { return &BenchmarkObserved{} },
}
