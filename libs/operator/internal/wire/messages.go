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

// DeviceObserved is the full observed state of one accelerator. Utilization, temperature and
// throughput are deliberately absent; see PROTOCOL §7.
type DeviceObserved struct {
	DeviceID         string            `json:"device_id"`
	NodeID           string            `json:"node_id"`
	ClusterID        string            `json:"cluster_id"`
	UUID             string            `json:"uuid"`
	Index            int               `json:"index"`
	Model            string            `json:"model"`
	DeviceClass      string            `json:"device_class"`
	MemoryTotalMB    int64             `json:"memory_total_mb"`
	MemoryUsedMB     int64             `json:"memory_used_mb"`
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

// TopologyObserved is one topology domain and the devices that share it.
type TopologyObserved struct {
	DomainID   string   `json:"domain_id"`
	ClusterID  string   `json:"cluster_id"`
	Kind       string   `json:"kind"`
	NodeID     *string  `json:"node_id,omitempty"`
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
	ReplicaID     string   `json:"replica_id"`
	DeploymentID  string   `json:"deployment_id"`
	ClusterID     string   `json:"cluster_id"`
	NodeID        string   `json:"node_id"`
	DeviceIDs     []string `json:"device_ids"`
	Phase         string   `json:"phase"`
	Ready         bool     `json:"ready"`
	Engine        string   `json:"engine"`
	EngineVersion string   `json:"engine_version"`
	RecipeID      string   `json:"recipe_id"`
	RestartCount  int      `json:"restart_count"`
	StartedAt     *string  `json:"started_at,omitempty"`
	ObservedAt    string   `json:"observed_at"`
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

// CacheObserved is the residency of one artifact against the declared warm-set.
type CacheObserved struct {
	ClusterID        string   `json:"cluster_id"`
	ArtifactID       string   `json:"artifact_id"`
	State            string   `json:"state"`
	Resident         bool     `json:"resident"`
	BytesResident    int64    `json:"bytes_resident"`
	NodeIDs          []string `json:"node_ids"`
	EvictionPressure float64  `json:"eviction_pressure"`
	LastAccessedAt   *string  `json:"last_accessed_at,omitempty"`
	ObservedAt       string   `json:"observed_at"`
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
}
