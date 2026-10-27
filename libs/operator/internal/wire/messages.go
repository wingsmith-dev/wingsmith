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

// StreamEnd terminates a sequence of frames sharing one ReplyTo, and closes a resync. A receiver
// prunes local state against a snapshot only when Complete is true; a truncated snapshot leaves
// local state untouched.
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

// ClusterObserved is what the cluster reports about itself as a whole. It is observed, never
// declared, and does not overwrite the plane's registry entry: the two disagreeing is worth seeing.
//
// Distribution is an opaque label the plane stores and displays and never branches on. ReadOnly
// tells the plane this operator applies nothing it declares, so the console can say so instead of
// a workload sitting at zero replicas unexplained. Multinode says a leader-worker backend (Grove or
// LeaderWorkerSet) is installed; placement reads it, and it is discovered from the API groups the
// cluster serves, not configured.
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

// NodeObserved is the full observed state of one node. Machine facts are in neutral units (cores,
// MiB), not orchestrator quantity strings, so the plane never parses Kubernetes resource grammar.
// Cloud is read per node from its provider reference, so a hybrid cluster reports each node truly.
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
// throughput and memory in use are measurements, not state, and are absent; see PROTOCOL §7.
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

// TopologyObserved is one topology domain, the devices that share it, and the nodes it spans. A
// domain such as an NVLink rack can span many nodes, and SpansNodes is the cluster's own answer.
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

// Overlay passes through engine configuration the serving spec does not model, limited to an
// allowlist on both the plane and the operator.
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

// ServingSpec is how to serve an artifact, inlined on each deployment so there is no ordering
// between the two. Digest is its identity: two deployments that would run the same process carry
// the same digest. The device shape is on the deployment's Placement, not here.
type ServingSpec struct {
	// Engine is which engine the worker launches inside the runtime: a module name and a flag
	// dialect, and no version at all.
	Engine string `json:"engine"`
	// EngineRuntimeVersion is a Dynamo runtime tag, e.g. 1.4.1, never the engine's own version.
	EngineRuntimeVersion string `json:"engine_runtime_version"`
	// EngineRuntimeImage is the whole address when the plane pins one instead of deriving it.
	EngineRuntimeImage *string `json:"engine_runtime_image"`

	// FrontendImage is the frontend's runtime when it must differ from the engine's, e.g. a patched
	// frontend over a stock engine. Nil means the cluster's default; a pointer so an older plane's
	// missing field does not read as a blank image.
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
	// Digest is `svg_` and a sha256 over the fields above, computed by the plane and never here.
	// It covers only what changes the running process.
	Digest  string  `json:"digest"`
	Overlay Overlay `json:"overlay"`

	// Topology is `aggregated` or `disaggregated`. Empty reads as aggregated, so an aggregated
	// deployment renders exactly as it did before the field existed.
	Topology string `json:"topology,omitempty"`

	// Roles is the per-role configuration of a disaggregated deployment, inside Digest. It carries
	// no counts: those are scale, in DeploymentDesired.RoleReplicas and outside the digest, so
	// adding a worker does not orphan the benchmarks linked to this configuration. Empty on an
	// aggregated deployment.
	Roles []RoleSpec `json:"roles,omitempty"`

	// Router is how this deployment's frontend routes between its workers. Nil, or every field nil,
	// renders plain KV routing, so the frontend pod template stays unchanged.
	Router *RouterSpec `json:"router,omitempty"`

	// OutputModality is `image` or `video` for a diffusion workload and empty (text) otherwise. It
	// renders as `--output-modalities` on the omni engine and is refused on any other.
	OutputModality string `json:"output_modality,omitempty"`

	// Diffusion is the per-deployment pipeline configuration of an omni worker, nil when none is
	// declared.
	Diffusion *DiffusionSpec `json:"diffusion,omitempty"`
}

// DiffusionSpec is what vLLM-Omni reads from a stage-config file. The operator writes the file and
// owns its fixed parts; the plane declares the pipeline class and sampling defaults.
type DiffusionSpec struct {
	// ModelClassName is the pipeline class, e.g. `LTX2Pipeline`, or empty to let omni infer it.
	ModelClassName string `json:"model_class_name,omitempty"`
	// DefaultSamplingParams are the stage's defaults, e.g. `num_inference_steps` and
	// `guidance_scale`. A request's own values still win.
	DefaultSamplingParams map[string]any `json:"default_sampling_params,omitempty"`
}

// RouterSpec is the frontend's router configuration. Each field is one `DYN_ROUTER_*` variable,
// validated by the plane; a nil field renders nothing and leaves Dynamo's default.
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

// RoleSpec is what makes one role's process differ from the other's in a disaggregated
// deployment, and nothing about how many of it run.
type RoleSpec struct {
	// Role is `prefill` or `decode`, validated by the plane.
	Role string `json:"role"`
	// MaxBatchTokens is this role's `--max-num-batched-tokens`, or nil to inherit the deployment's.
	MaxBatchTokens *int `json:"max_batch_tokens,omitempty"`
	// EngineArgs is laid over the deployment's own. The plane refuses flags the operator derives,
	// including `disaggregation-mode` and `kv-transfer-config`.
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

// WorkloadDiscovered is a workload found running in the cluster that the plane never declared. It
// is a sighting only: nothing reconciles it.
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

// WorkloadSpec is how a discovered workload was told to serve, carried uninterpreted. EnvNames
// holds names without values, so credentials in a pod's environment never reach the plane.
type WorkloadSpec struct {
	Image        string            `json:"image,omitempty"`
	Command      []string          `json:"command,omitempty"`
	Args         []string          `json:"args,omitempty"`
	EnvNames     []string          `json:"env_names,omitempty"`
	Requests     map[string]string `json:"requests,omitempty"`
	Limits       map[string]string `json:"limits,omitempty"`
	NodeSelector map[string]string `json:"node_selector,omitempty"`
}

// Adopt marks a DeploymentDesired promoted from a discovered workload: the operator takes
// ownership of the running object in place instead of applying a render over it. A live object
// whose fingerprint no longer matches SpecFingerprint is refused, since converging it would
// restart a model nobody asked to restart.
type Adopt struct {
	Namespace       string `json:"namespace"`
	Kind            string `json:"kind"`
	Name            string `json:"name"`
	SpecFingerprint string `json:"spec_fingerprint"`
}

// DeploymentDesired is the declared state of one deployment. Full object, never a patch.
type DeploymentDesired struct {
	DeploymentID  string `json:"deployment_id"`
	Name          string `json:"name,omitempty"`
	ClusterID     string `json:"cluster_id"`
	ProjectID     string `json:"project_id"`
	Lifecycle     string `json:"lifecycle"`
	PriorityClass string `json:"priority_class"`
	// MinReplicas is this cluster's share of the workload's scale, rendered exactly as sent and never
	// summed or derived against anything else. MaxReplicas is reserved for a future autoscaler.
	MinReplicas int `json:"min_replicas"`
	MaxReplicas int `json:"max_replicas"`
	// RoleReplicas is the absolute worker count per role of a disaggregated deployment, keyed like
	// Serving.Roles. MinReplicas counts groups there, and the plane does the multiplication so the
	// operator never does arithmetic on a replica count. Empty on an aggregated deployment.
	RoleReplicas map[string]int       `json:"role_replicas,omitempty"`
	Placement    PlacementConstraints `json:"placement"`
	// Serving is inlined, so it always arrives with the deployment.
	Serving     ServingSpec `json:"serving"`
	ArtifactIDs []string    `json:"artifact_ids"`
	// ServedModelName is the model id clients send, resolved by the plane and never empty.
	// Serving.EngineArgs can still override it.
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

// RoleObserved is one role's share of a deployment's rollout. Desired echoes role_replicas.
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

	// ReplicaIDs is every replica this deployment has here now, which lets the plane forget the
	// rest. Not omitempty: nil is an older operator making no claim, empty says there are none.
	ReplicaIDs []string `json:"replica_ids"`

	// Reason is the sub-state within Phase, from the closed set in MESSAGES.md; usually an
	// ordering not yet satisfied, not an error.
	Reason string `json:"reason,omitempty"`

	// Message is the same fact written for a person, and is never parsed.
	Message string `json:"message,omitempty"`

	// Roles splits the rollout by role, beside the scalars, which stay the group rollup an older
	// plane reads. They show which role is short, e.g. ready decode workers with no prefill.
	// Omitted on an aggregated deployment.
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
	// Role is `prefill` or `decode` on a disaggregated deployment, read from the pod label the
	// renderer stamps, never from the pod name. Omitted when empty.
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
	// Reason and Message say why this state, e.g. a download not started versus one nothing here
	// can start. Omitted when empty.
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

// OperationCancel withdraws an operation already sent. A frame of its own, so a resync cannot
// prune it. The receiver reports a terminal phase for OperationID; an unknown id is not an error,
// so cancellation is at-least-once and idempotent.
type OperationCancel struct {
	OperationID string `json:"operation_id"`
	ClusterID   string `json:"cluster_id"`
	Reason      string `json:"reason"`
	RequestedBy string `json:"requested_by"`
	RequestedAt string `json:"requested_at"`
}

// BenchmarkObserved is the outcome of a benchmark run, beside OperationObserved because a harness
// emits nested JSON. Summary is the harness's own export, forwarded as is; HarnessVersion is what
// makes two runs comparable.
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
	// LogTail is the capped last few KiB of the harness's stdout, where a failure shows. Full logs
	// belong at ArtifactsURI.
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
