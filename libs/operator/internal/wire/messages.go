package wire

// ---------------------------------------------------------------------------- session

// Hello is the first frame the operator sends. It is never sent twice on one connection.
type Hello struct {
	ClusterID    string  `json:"cluster_id"`
	AgentVersion string  `json:"agent_version"`
	Lossy        bool    `json:"lossy"`
	ResumeFrom   *uint64 `json:"resume_from,omitempty"`
	SessionID    *string `json:"session_id,omitempty"`
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

// NodeCondition is one observed condition on a node. Cordon state is not a condition; it is the
// separate observed boolean NodeObserved.Cordoned.
type NodeCondition struct {
	Type    string `json:"type"`
	Status  string `json:"status"`
	Reason  string `json:"reason"`
	Message string `json:"message"`
	Since   string `json:"since"`
}

// NodeObserved is the full observed state of one node.
type NodeObserved struct {
	NodeID         string            `json:"node_id"`
	ClusterID      string            `json:"cluster_id"`
	Name           string            `json:"name"`
	Pool           string            `json:"pool"`
	Membership     string            `json:"membership"`
	Ready          bool              `json:"ready"`
	Cordoned       bool              `json:"cordoned"`
	Conditions     []NodeCondition   `json:"conditions"`
	KernelVersion  string            `json:"kernel_version"`
	DriverVersion  string            `json:"driver_version"`
	RuntimeVersion string            `json:"runtime_version"`
	AgentVersion   string            `json:"agent_version"`
	DeviceIDs      []string          `json:"device_ids"`
	Labels         map[string]string `json:"labels"`
	ObservedAt     string            `json:"observed_at"`
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

// RecipeOverlay is the escape hatch for what this plane does not model.
//
// Engines and their operators carry far more configuration than a control plane wants to mirror
// field by field, and mirroring it is a losing race. The recipe models what the plane reasons
// about and passes the rest through.
type RecipeOverlay struct {
	// PodSpec is merged over the rendered pod with strategic-merge semantics, so containers,
	// env, volumes and volume mounts merge by name.
	PodSpec map[string]any `json:"pod_spec"`

	// Dynamo is merged over the rendered graph deployment. No schema is available to read merge
	// keys from, so maps merge and lists replace.
	Dynamo map[string]any `json:"dynamo"`
}

// RecipeParallelism is how one replica is split across its devices.
type RecipeParallelism struct {
	Tensor   int `json:"tensor"`
	Pipeline int `json:"pipeline"`
	Expert   int `json:"expert"`
}

// RecipeResources is the non-accelerator footprint of one replica.
type RecipeResources struct {
	CPUCores  *int   `json:"cpu_cores,omitempty"`
	MemoryMiB *int64 `json:"memory_mib,omitempty"`
	DiskGiB   *int   `json:"disk_gib,omitempty"`
}

// RecipeDesired is how to serve an artifact, in enough detail for a cluster to render it.
//
// The same split KServe draws between ServingRuntime and InferenceService: this object is the
// engine and its configuration, and the deployment referencing it supplies scale and placement.
// Recipes are shared, so they travel as their own declared object rather than inlined on every
// deployment that names them.
//
// The device shape is deliberately absent — it arrives on the deployment's Placement, derived from
// this recipe, and carrying it twice invites the two copies to disagree.
type RecipeDesired struct {
	RecipeID           string            `json:"recipe_id"`
	ClusterID          string            `json:"cluster_id"`
	ArtifactID         string            `json:"artifact_id"`
	Name               string            `json:"name"`
	Version            int               `json:"version"`
	Engine             string            `json:"engine"`
	EngineVersion      string            `json:"engine_version"`
	EngineImage        *string           `json:"engine_image"`
	EngineArgs         map[string]string `json:"engine_args"`
	Quantization       *string           `json:"quantization"`
	Dtype              *string           `json:"dtype"`
	Parallelism        RecipeParallelism `json:"parallelism"`
	ContextLength      int               `json:"context_length"`
	MaxBatchTokens     *int              `json:"max_batch_tokens"`
	KVCacheDtype       *string           `json:"kv_cache_dtype"`
	KVCacheFraction    *float64          `json:"kv_cache_fraction"`
	PrefixCacheEnabled bool              `json:"prefix_cache_enabled"`
	PrefixCacheConfig  map[string]string `json:"prefix_cache_config"`
	Resources          RecipeResources   `json:"resources"`
	Labels             map[string]string `json:"labels"`
	UpdatedAt          string            `json:"updated_at"`
	Overlay            RecipeOverlay     `json:"overlay"`
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
	DeploymentID   string               `json:"deployment_id"`
	ClusterID      string               `json:"cluster_id"`
	ProjectID      string               `json:"project_id"`
	RecipeID       string               `json:"recipe_id"`
	RecipeRevision int                  `json:"recipe_revision"`
	Lifecycle      string               `json:"lifecycle"`
	PriorityClass  string               `json:"priority_class"`
	MinReplicas    int                  `json:"min_replicas"`
	MaxReplicas    int                  `json:"max_replicas"`
	Placement      PlacementConstraints `json:"placement"`
	ArtifactIDs    []string             `json:"artifact_ids"`
	Generation     int64                `json:"generation"`
	UpdatedAt      string               `json:"updated_at"`
	Adopt          *Adopt               `json:"adopt,omitempty"`
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
// Recipe configuration.
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
}

// ReplicaObserved is one running instance on named devices.
type ReplicaObserved struct {
	ReplicaID          string   `json:"replica_id"`
	Name               string   `json:"name"`
	DeploymentID       string   `json:"deployment_id"`
	ClusterID          string   `json:"cluster_id"`
	NodeID             string   `json:"node_id"`
	DeviceIDs          []string `json:"device_ids"`
	Phase              string   `json:"phase"`
	Ready              bool     `json:"ready"`
	Engine             string   `json:"engine"`
	EngineVersion      string   `json:"engine_version"`
	RecipeID           string   `json:"recipe_id"`
	RestartCount       int      `json:"restart_count"`
	ObservedGeneration int64    `json:"observed_generation"`
	StartedAt          *string  `json:"started_at,omitempty"`
	ObservedAt         string   `json:"observed_at"`
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
	"node.observed":       func() any { return &NodeObserved{} },
	"node.event":          func() any { return &NodeEvent{} },
	"device.observed":     func() any { return &DeviceObserved{} },
	"device.event":        func() any { return &DeviceEvent{} },
	"topology.observed":   func() any { return &TopologyObserved{} },
	"capacity.observed":   func() any { return &CapacityObserved{} },
	"workload.discovered": func() any { return &WorkloadDiscovered{} },
	"deployment.desired":  func() any { return &DeploymentDesired{} },
	"deployment.deleted":  func() any { return &DeploymentDeleted{} },
	"recipe.desired":      func() any { return &RecipeDesired{} },
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
}
