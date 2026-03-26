package api

import "time"

// Page is the envelope every list response is wrapped in. `count` is the size
// of this page, not of the collection, and `next_cursor` is absent on the last
// page — a listing without one is complete, and a listing with one is not.
type Page[T any] struct {
	Items      []T    `json:"items"`
	Count      int    `json:"count"`
	NextCursor string `json:"next_cursor,omitempty"`
}

// ListOpts is every filter the API accepts, in one struct. Each list call
// sends only the dimensions its own endpoint declares; a value set for a
// dimension an endpoint does not have is dropped rather than guessed at.
type ListOpts struct {
	Limit  int
	Cursor string
	Search string
	Labels map[string]string

	// All follows `next_cursor` until the collection is exhausted, so a
	// listing is the whole collection rather than its first page.
	All bool

	Cluster        string
	Pool           string
	Node           string
	Class          string
	Health         string
	Phase          string
	Region         string
	Project        string
	Tenant         string
	Kind           string
	Type           string
	Format         string
	Name           string
	Engine         string
	Artifact       string
	Recipe         string
	Condition      string
	TopologyDomain string
	Target         string
	Actor          string
	Subject        string
	Severity       string
	Device         string
	Replica        string
	Namespace      string
	Status         string

	Ready            *bool
	Cordoned         *bool
	Allocatable      *bool
	Enabled          *bool
	Behind           *bool
	SpansNodes       *bool
	OverQuota        *bool
	Live             *bool
	Preemptible      *bool
	Preempts         *bool
	OverQuotaEnabled *bool

	Since *time.Time
	Until *time.Time
}

// Identity is what the CLI can say about the caller. The plane has no
// `/v1/whoami`: authentication is stubbed to one static token, so identity is
// the configured context plus whether the server answers at all.
type Identity struct {
	Server    string `json:"server"`
	Project   string `json:"project,omitempty"`
	HasToken  bool   `json:"has_token"`
	Reachable bool   `json:"reachable"`
	Simulated bool   `json:"simulated"`
}

// --- supply ----------------------------------------------------------------

// ClusterSync is the observed liveness of a cluster's agent link. `state` is
// derived by the plane: never-connected and connected-then-dropped are not the
// same fact.
type ClusterSync struct {
	State        string     `json:"state"`
	LastSyncAt   *time.Time `json:"last_sync_at"`
	LastSeenAt   *time.Time `json:"last_seen_at"`
	AgentVersion *string    `json:"agent_version"`
	LagMS        *int       `json:"lag_ms"`
	LastError    *string    `json:"last_error"`
}

// Cluster is the declared enrolment record and the observed link state,
// returned together and never merged: `phase` is what we asked for,
// `reachable` and `sync` are what is true now.
type Cluster struct {
	ID           string         `json:"id"`
	Name         string         `json:"name"`
	DisplayName  *string        `json:"display_name"`
	Region       string         `json:"region"`
	Zone         *string        `json:"zone"`
	Residency    *string        `json:"residency"`
	Phase        string         `json:"phase"`
	Labels       map[string]any `json:"labels"`
	Capabilities map[string]any `json:"capabilities"`
	EnrolledAt   *time.Time     `json:"enrolled_at"`
	CreatedAt    time.Time      `json:"created_at"`
	UpdatedAt    *time.Time     `json:"updated_at"`
	Revision     string         `json:"revision"`
	Reachable    bool           `json:"reachable"`
	Sync         ClusterSync    `json:"sync"`
}

// Cluster phases. Enrolment and reachability are different questions, so a
// cluster is never "offline" as a phase — it is active and unreachable.
const (
	PhasePending  = "pending"
	PhaseActive   = "active"
	PhaseDraining = "draining"
	PhaseDisabled = "disabled"
)

// NodeDeviceRollup is how many devices a node has and how they split by
// health. State only — no utilization, no temperature, no power.
type NodeDeviceRollup struct {
	Total    int            `json:"total"`
	ByHealth map[string]int `json:"by_health"`
}

// Healthy is the count of devices the operator reports as healthy.
func (r NodeDeviceRollup) Healthy() int { return r.ByHealth[HealthHealthy] }

// Unhealthy is every device that is not healthy, whatever it is instead.
func (r NodeDeviceRollup) Unhealthy() int { return r.Total - r.Healthy() }

// Node is a machine observed inside a cluster. Everything on it is observed;
// `cordoned` is a boolean beside `condition` and never a value of it, because
// draining is an Operation and the cordon is the state it leaves behind.
type Node struct {
	ID            string           `json:"id"`
	Name          string           `json:"name"`
	Cluster       string           `json:"cluster"`
	ClusterName   string           `json:"cluster_name"`
	Pool          *string          `json:"pool"`
	PoolName      *string          `json:"pool_name"`
	Condition     string           `json:"condition"`
	Cordoned      bool             `json:"cordoned"`
	Ready         bool             `json:"ready"`
	CPUCores      *int             `json:"cpu_cores"`
	MemoryMiB     *int             `json:"memory_mib"`
	CPUArch       *string          `json:"cpu_arch"`
	OSImage       *string          `json:"os_image"`
	KernelVersion *string          `json:"kernel_version"`
	DriverVersion *string          `json:"driver_version"`
	AgentVersion  *string          `json:"agent_version"`
	Labels        map[string]any   `json:"labels"`
	Taints        []any            `json:"taints"`
	Devices       NodeDeviceRollup `json:"devices"`
	JoinedAt      *time.Time       `json:"joined_at"`
	ObservedAt    *time.Time       `json:"observed_at"`
	CreatedAt     time.Time        `json:"created_at"`
	UpdatedAt     *time.Time       `json:"updated_at"`
}

// DeviceTopology is the device's topology domain inlined for display. It is a
// summary and never the authority: no parent, no membership, no counts.
type DeviceTopology struct {
	ID         string `json:"id"`
	Kind       string `json:"kind"`
	Identifier string `json:"identifier"`
}

// Device is one accelerator — the schedulable unit, and always observed.
// Health, ECC, throttle, MIG and allocatability are state; utilization and
// temperature are telemetry and never appear here.
type Device struct {
	ID                  string          `json:"id"`
	Cluster             string          `json:"cluster"`
	ClusterName         string          `json:"cluster_name"`
	Node                string          `json:"node"`
	NodeName            string          `json:"node_name"`
	DeviceUUID          string          `json:"device_uuid"`
	DeviceIndex         int             `json:"device_index"`
	DeviceClass         string          `json:"device_class"`
	Vendor              string          `json:"vendor"`
	ModelName           *string         `json:"model_name"`
	PCIAddress          *string         `json:"pci_address"`
	MemoryMiB           int             `json:"memory_mib"`
	ComputeCapability   *string         `json:"compute_capability"`
	Health              string          `json:"health"`
	HealthReason        *string         `json:"health_reason"`
	ECCEnabled          bool            `json:"ecc_enabled"`
	ECCVolatileErrors   int             `json:"ecc_volatile_errors"`
	ECCAggregateErrors  int             `json:"ecc_aggregate_errors"`
	ECCPendingRetire    bool            `json:"ecc_pending_retirement"`
	Throttled           bool            `json:"throttled"`
	ThrottleReasons     []string        `json:"throttle_reasons"`
	MIGEnabled          bool            `json:"mig_enabled"`
	MIGProfile          *string         `json:"mig_profile"`
	MIGParentDeviceUUID *string         `json:"mig_parent_device_uuid"`
	Allocatable         bool            `json:"allocatable"`
	Topology            *DeviceTopology `json:"topology"`
	ObservedAt          *time.Time      `json:"observed_at"`
	CreatedAt           time.Time       `json:"created_at"`
	UpdatedAt           *time.Time      `json:"updated_at"`
}

// TopologyDomain is a set of devices that share an interconnect, and the
// authority on topology: nesting and membership are answered here and nowhere
// else.
type TopologyDomain struct {
	ID                string         `json:"id"`
	Cluster           string         `json:"cluster"`
	ClusterName       string         `json:"cluster_name"`
	Node              *string        `json:"node"`
	NodeName          *string        `json:"node_name"`
	Parent            *string        `json:"parent"`
	Kind              string         `json:"kind"`
	Identifier        string         `json:"identifier"`
	LinkGeneration    *string        `json:"link_generation"`
	LinkBandwidthGbps *int           `json:"link_bandwidth_gbps"`
	DeviceCount       int            `json:"device_count"`
	SpansNodes        bool           `json:"spans_nodes"`
	Attributes        map[string]any `json:"attributes"`
	ObservedAt        *time.Time     `json:"observed_at"`
	CreatedAt         time.Time      `json:"created_at"`
	UpdatedAt         *time.Time     `json:"updated_at"`
}

// PoolMembership is the observed half of a pool: a rollup, never a list.
type PoolMembership struct {
	NodesTotal         int `json:"nodes_total"`
	NodesReady         int `json:"nodes_ready"`
	NodesCordoned      int `json:"nodes_cordoned"`
	DevicesTotal       int `json:"devices_total"`
	DevicesHealthy     int `json:"devices_healthy"`
	DevicesAllocatable int `json:"devices_allocatable"`
}

// Pool is a declared provisioning spec beside its observed membership.
type Pool struct {
	ID                  string         `json:"id"`
	ClusterID           string         `json:"cluster_id"`
	Name                string         `json:"name"`
	DeviceClass         string         `json:"device_class"`
	DevicesPerNode      int            `json:"devices_per_node"`
	NodeShape           map[string]any `json:"node_shape"`
	MinNodes            int            `json:"min_nodes"`
	MaxNodes            int            `json:"max_nodes"`
	Labels              map[string]any `json:"labels"`
	Taints              []any          `json:"taints"`
	Enabled             bool           `json:"enabled"`
	CreatedAt           time.Time      `json:"created_at"`
	UpdatedAt           *time.Time     `json:"updated_at"`
	Revision            string         `json:"revision"`
	Membership          PoolMembership `json:"membership"`
	WithinDeclaredBound bool           `json:"within_declared_bounds"`
}

// Device health and node conditions, as the plane spells them.
const (
	HealthHealthy   = "healthy"
	HealthDegraded  = "degraded"
	HealthUnhealthy = "unhealthy"
	HealthUnknown   = "unknown"

	NodeReady    = "ready"
	NodeNotReady = "not_ready"
	NodeUnknown  = "unknown"
)

// --- demand ----------------------------------------------------------------

// ArtifactLocation is one place an artifact's bytes can be fetched from. It
// has no id: the uri is unique within the artifact and is the handle.
type ArtifactLocation struct {
	Scheme    string  `json:"scheme"`
	URI       string  `json:"uri"`
	Region    *string `json:"region"`
	Preferred bool    `json:"preferred"`
}

// Artifact is immutable, content-addressed bytes. Identity is the digest.
type Artifact struct {
	ID             string             `json:"id"`
	Digest         string             `json:"digest"`
	Name           string             `json:"name"`
	Kind           string             `json:"kind"`
	Format         *string            `json:"format"`
	SizeBytes      int64              `json:"size_bytes"`
	Licence        *string            `json:"licence"`
	ParameterCount *int64             `json:"parameter_count"`
	BaseArtifactID *string            `json:"base_artifact_id"`
	ProjectID      *string            `json:"project_id"`
	Metadata       map[string]any     `json:"metadata"`
	CreatedAt      time.Time          `json:"created_at"`
	UpdatedAt      *time.Time         `json:"updated_at"`
	Revision       string             `json:"revision"`
	Locations      []ArtifactLocation `json:"locations"`
}

// RecipeParallelism is the tensor/pipeline/expert split.
type RecipeParallelism struct {
	Tensor   int `json:"tensor"`
	Pipeline int `json:"pipeline"`
	Expert   int `json:"expert"`
}

// RecipeCache is KV and prefix cache configuration, unrelated to the cluster
// weight cache.
type RecipeCache struct {
	KVCacheDtype       *string        `json:"kv_cache_dtype"`
	KVCacheFraction    *float64       `json:"kv_cache_fraction"`
	PrefixCacheEnabled bool           `json:"prefix_cache_enabled"`
	PrefixCacheConfig  map[string]any `json:"prefix_cache_config"`
}

// RecipeShape is the hardware a recipe implies, class first: a count without
// a class is not a shape.
type RecipeShape struct {
	DeviceClass                  string  `json:"device_class"`
	DeviceCount                  int     `json:"device_count"`
	DeviceMemoryMiBMin           int     `json:"device_memory_mib_min"`
	RequiresSingleTopologyDomain bool    `json:"requires_single_topology_domain"`
	TopologyDomainKind           *string `json:"topology_domain_kind"`
}

// RecipeResources is the non-accelerator footprint.
type RecipeResources struct {
	CPUCores  *int `json:"cpu_cores"`
	MemoryMiB *int `json:"memory_mib"`
	DiskGiB   *int `json:"disk_gib"`
}

// Recipe is a reproducible way to serve one artifact.
type Recipe struct {
	ID             string            `json:"id"`
	ArtifactID     string            `json:"artifact_id"`
	Name           string            `json:"name"`
	Version        int               `json:"version"`
	Engine         string            `json:"engine"`
	EngineVersion  string            `json:"engine_version"`
	EngineImage    *string           `json:"engine_image"`
	EngineArgs     map[string]any    `json:"engine_args"`
	Quantization   *string           `json:"quantization"`
	Dtype          *string           `json:"dtype"`
	Parallelism    RecipeParallelism `json:"parallelism"`
	ContextLength  int               `json:"context_length"`
	MaxBatchTokens *int              `json:"max_batch_tokens"`
	Cache          RecipeCache       `json:"cache"`
	Shape          RecipeShape       `json:"shape"`
	Resources      RecipeResources   `json:"resources"`
	Labels         map[string]any    `json:"labels"`
	CreatedAt      time.Time         `json:"created_at"`
	UpdatedAt      *time.Time        `json:"updated_at"`
	Revision       string            `json:"revision"`
}

// DeploymentWorkload is the scheduling supertype half: what is scheduled, at
// what priority, and how far it got.
type DeploymentWorkload struct {
	Type          string     `json:"type"`
	Phase         string     `json:"phase"`
	Reason        *string    `json:"reason"`
	ProjectID     string     `json:"project_id"`
	PriorityClass string     `json:"priority_class"`
	Preemptible   bool       `json:"preemptible"`
	GangSize      int        `json:"gang_size"`
	QueuedAt      *time.Time `json:"queued_at"`
	AdmittedAt    *time.Time `json:"admitted_at"`
	PlacedAt      *time.Time `json:"placed_at"`
	StartedAt     *time.Time `json:"started_at"`
	DeadlineAt    *time.Time `json:"deadline_at"`
}

// DeploymentReplicaRollup counts observed replicas rather than listing them.
type DeploymentReplicaRollup struct {
	Total               int `json:"total"`
	Ready               int `json:"ready"`
	Running             int `json:"running"`
	Pending             int `json:"pending"`
	Failed              int `json:"failed"`
	AtCurrentGeneration int `json:"at_current_generation"`
}

// Deployment is a recipe placed on a cluster: declared spec, the workload it
// specialises, and the replicas the cluster reports.
type Deployment struct {
	ID                 string                  `json:"id"`
	Name               string                  `json:"name"`
	ClusterID          string                  `json:"cluster_id"`
	RecipeID           string                  `json:"recipe_id"`
	MinReplicas        int                     `json:"min_replicas"`
	MaxReplicas        int                     `json:"max_replicas"`
	DesiredReplicas    int                     `json:"desired_replicas"`
	Generation         int                     `json:"generation"`
	Enabled            bool                    `json:"enabled"`
	AdapterArtifactIDs []string                `json:"adapter_artifact_ids"`
	Overrides          map[string]any          `json:"overrides"`
	Labels             map[string]any          `json:"labels"`
	CreatedAt          time.Time               `json:"created_at"`
	UpdatedAt          *time.Time              `json:"updated_at"`
	Revision           string                  `json:"revision"`
	AdoptedWorkloadID  *string                 `json:"adopted_workload_id"`
	AdoptedFingerprint *string                 `json:"adopted_fingerprint"`
	Workload           DeploymentWorkload      `json:"workload"`
	Replicas           DeploymentReplicaRollup `json:"replicas"`
}

// DiscoveredWorkload is something already running in a cluster that the plane
// never declared. Status is `new` until somebody decides: `tracked` puts it in
// the inventory, `ignored` takes it out of the list.
//
// There is no generation and no recipe here, and that is the point. Adopting one
// records a decision and declares nothing, so nothing reconciles and nothing
// restarts — a model whose weights are already resident stays that way.
type DiscoveredWorkload struct {
	ID                string         `json:"id"`
	ClusterID         string         `json:"cluster_id"`
	ClusterName       string         `json:"cluster_name"`
	Namespace         string         `json:"namespace"`
	Name              string         `json:"name"`
	Kind              string         `json:"kind"`
	Model             *string        `json:"model"`
	Images            []string       `json:"images"`
	ReplicasDesired   int            `json:"replicas_desired"`
	ReplicasReady     int            `json:"replicas_ready"`
	DevicesPerReplica int            `json:"devices_per_replica"`
	DevicesTotal      int            `json:"devices_total"`
	NodeIDs           []string       `json:"node_ids"`
	Labels            map[string]any `json:"labels"`
	SpecFingerprint   *string        `json:"spec_fingerprint"`
	Status            string         `json:"status"`
	DeploymentID      *string        `json:"deployment_id"`
	ProjectID         *string        `json:"project_id"`
	Note              *string        `json:"note"`
	DecidedBy         *string        `json:"decided_by"`
	DecidedAt         *time.Time     `json:"decided_at"`
	FirstSeenAt       *time.Time     `json:"first_seen_at"`
	ObservedAt        *time.Time     `json:"observed_at"`
}

// DecisionRequest is what track and ignore send. It carries nothing a cluster
// could act on, which is what makes adoption safe.
type DecisionRequest struct {
	Project string `json:"project,omitempty"`
	Note    string `json:"note,omitempty"`
}

// PromoteRequest turns a sighting into a deployment the plane manages.
//
// Fingerprint is the digest the caller was shown. The plane refuses the promotion if the workload
// has changed since, so an adoption is never pinned to a spec nobody looked at. There is no field
// here for the image, the overrides or the replica count: anything that could change what the
// workload is running would make promotion capable of restarting it.
type PromoteRequest struct {
	Recipe        string `json:"recipe"`
	Project       string `json:"project"`
	Fingerprint   string `json:"fingerprint"`
	Name          string `json:"name,omitempty"`
	PriorityClass string `json:"priority_class,omitempty"`
}

// EndpointTarget is one deployment behind one endpoint, with a traffic
// weight. Which cluster it runs in is a property of the deployment.
type EndpointTarget struct {
	DeploymentID string `json:"deployment_id"`
	Weight       int    `json:"weight"`
	Role         string `json:"role"`
	Enabled      bool   `json:"enabled"`
}

// Endpoint is the stable public name clients call. Global and plane-only:
// there is no cluster on it at any depth.
type Endpoint struct {
	ID              string           `json:"id"`
	Name            string           `json:"name"`
	DisplayName     *string          `json:"display_name"`
	ProjectID       string           `json:"project_id"`
	Visibility      string           `json:"visibility"`
	RoutingStrategy string           `json:"routing_strategy"`
	Enabled         bool             `json:"enabled"`
	Targets         []EndpointTarget `json:"targets"`
	FailoverOrder   []string         `json:"failover_order"`
	Labels          map[string]any   `json:"labels"`
	CreatedAt       time.Time        `json:"created_at"`
	UpdatedAt       *time.Time       `json:"updated_at"`
	Revision        string           `json:"revision"`
}

// --- binding ---------------------------------------------------------------

// ProjectTenant is the tenant a project belongs to, embedded rather than
// linked: there is no `/v1/tenants` collection.
type ProjectTenant struct {
	ID      string `json:"id"`
	Slug    string `json:"slug"`
	Name    string `json:"name"`
	Enabled bool   `json:"enabled"`
}

// Project is the tenancy unit `X-Hangar-Project` names.
type Project struct {
	Tenant               ProjectTenant `json:"tenant"`
	ID                   string        `json:"id"`
	Slug                 string        `json:"slug"`
	Name                 string        `json:"name"`
	DefaultPriorityClass *string       `json:"default_priority_class"`
	Enabled              bool          `json:"enabled"`
	EffectiveEnabled     bool          `json:"effective_enabled"`
	CreatedAt            time.Time     `json:"created_at"`
	UpdatedAt            *time.Time    `json:"updated_at"`
	Revision             string        `json:"revision"`
}

// QuotaUsage is the observed half of a quota, counted over live allocations.
type QuotaUsage struct {
	AllocatedDevices int `json:"allocated_devices"`
	LiveAllocations  int `json:"live_allocations"`
	OverQuotaDevices int `json:"over_quota_devices"`
}

// Quota is a guaranteed floor and a fair share with usage beside it, never
// merged. `cluster` is null when the quota spans every cluster.
type Quota struct {
	ID                string     `json:"id"`
	Project           string     `json:"project"`
	ProjectSlug       string     `json:"project_slug"`
	Cluster           *string    `json:"cluster"`
	ClusterName       *string    `json:"cluster_name"`
	DeviceClass       string     `json:"device_class"`
	GuaranteedDevices int        `json:"guaranteed_devices"`
	LimitDevices      *int       `json:"limit_devices"`
	OverQuotaWeight   float64    `json:"over_quota_weight"`
	OverQuotaEnabled  bool       `json:"over_quota_enabled"`
	CreatedAt         time.Time  `json:"created_at"`
	UpdatedAt         *time.Time `json:"updated_at"`
	Revision          string     `json:"revision"`
	Usage             QuotaUsage `json:"usage"`
	HeadroomDevices   *int       `json:"headroom_devices"`
	OverLimit         bool       `json:"over_limit"`
}

// PriorityClass is what preempts what. `preemptible` and `preempts` are two
// switches and not one axis.
type PriorityClass struct {
	ID          string     `json:"id"`
	Name        string     `json:"name"`
	Value       int        `json:"value"`
	Preemptible bool       `json:"preemptible"`
	Preempts    bool       `json:"preempts"`
	IsDefault   bool       `json:"is_default"`
	Description *string    `json:"description"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   *time.Time `json:"updated_at"`
	Revision    string     `json:"revision"`
}

// PolicyAttachment is one subject a policy binds to. `subject_id` is the raw
// polymorphic key and survives the subject being deleted.
type PolicyAttachment struct {
	SubjectType string  `json:"subject_type"`
	SubjectID   string  `json:"subject_id"`
	Subject     *string `json:"subject"`
	SubjectName *string `json:"subject_name"`
}

// Policy is a placement constraint and everything it currently binds to. An
// empty allow-list means "no opinion", not "nothing allowed".
type Policy struct {
	ID                   string             `json:"id"`
	Name                 string             `json:"name"`
	Description          *string            `json:"description"`
	AllowedRegions       []string           `json:"allowed_regions"`
	AllowedResidencies   []string           `json:"allowed_residencies"`
	AllowedDeviceClasses []string           `json:"allowed_device_classes"`
	AllowedClusters      []string           `json:"allowed_clusters"`
	DeniedClusters       []string           `json:"denied_clusters"`
	Rules                map[string]any     `json:"rules"`
	Enabled              bool               `json:"enabled"`
	CreatedAt            time.Time          `json:"created_at"`
	UpdatedAt            *time.Time         `json:"updated_at"`
	Revision             string             `json:"revision"`
	Attachments          []PolicyAttachment `json:"attachments"`
}

// --- machinery -------------------------------------------------------------

// AllocationDevice is one device inside one allocation. Released members are
// returned rather than filtered out; `live` says which side of the line.
type AllocationDevice struct {
	Device     string     `json:"device"`
	DeviceUUID string     `json:"device_uuid"`
	Node       string     `json:"node"`
	Live       bool       `json:"live"`
	ReleasedAt *time.Time `json:"released_at"`
}

// Allocation is a set of devices committed together to one workload. Every
// field is written by ingestion; there is no declared half.
type Allocation struct {
	ID           string             `json:"id"`
	Cluster      string             `json:"cluster"`
	ClusterName  string             `json:"cluster_name"`
	Project      string             `json:"project"`
	ProjectSlug  string             `json:"project_slug"`
	Workload     string             `json:"workload"`
	Replica      *string            `json:"replica"`
	DeviceClass  string             `json:"device_class"`
	DeviceCount  int                `json:"device_count"`
	OverQuota    bool               `json:"over_quota"`
	Phase        string             `json:"phase"`
	Live         bool               `json:"live"`
	GangComplete bool               `json:"gang_complete"`
	AcquiredAt   *time.Time         `json:"acquired_at"`
	ReleasedAt   *time.Time         `json:"released_at"`
	ObservedAt   *time.Time         `json:"observed_at"`
	CreatedAt    time.Time          `json:"created_at"`
	UpdatedAt    *time.Time         `json:"updated_at"`
	Devices      []AllocationDevice `json:"devices"`
}

// OperationTarget is what an operation acts upon, as a kind and an id. The
// kind is explicit so no client has to parse an id to know what it is.
type OperationTarget struct {
	Type string `json:"type"`
	ID   string `json:"id"`
}

// Operation is an imperative action with a lifecycle, not a verb that
// returned 200: it has an actor, a reason, attempts, and a terminal phase.
type Operation struct {
	ID                string          `json:"id"`
	ClusterID         *string         `json:"cluster_id"`
	Type              string          `json:"type"`
	Target            OperationTarget `json:"target"`
	Reason            *string         `json:"reason"`
	RequestedBy       string          `json:"requested_by"`
	Parameters        map[string]any  `json:"parameters"`
	DeadlineAt        *time.Time      `json:"deadline_at"`
	CreatedAt         time.Time       `json:"created_at"`
	UpdatedAt         *time.Time      `json:"updated_at"`
	Phase             string          `json:"phase"`
	Result            map[string]any  `json:"result"`
	Error             *string         `json:"error"`
	Attempts          int             `json:"attempts"`
	CancelRequestedAt *time.Time      `json:"cancel_requested_at"`
	StartedAt         *time.Time      `json:"started_at"`
	FinishedAt        *time.Time      `json:"finished_at"`
}

// Operation phases. Terminal ones are what `hgr operation wait` waits for.
const (
	OpPending   = "pending"
	OpRunning   = "running"
	OpSucceeded = "succeeded"
	OpFailed    = "failed"
	OpCancelled = "cancelled"
	OpExpired   = "expired"
)

// Terminal reports whether an operation has stopped moving.
func (o Operation) Terminal() bool {
	switch o.Phase {
	case OpSucceeded, OpFailed, OpCancelled, OpExpired:
		return true
	}
	return false
}

// OperationRequest creates an Operation: a type and a target, never a verb on
// the target's own path. `cluster` is accepted only for targets that do not
// carry a cluster of their own.
type OperationRequest struct {
	Type       string         `json:"type"`
	Target     string         `json:"target"`
	Reason     string         `json:"reason,omitempty"`
	Deadline   *time.Time     `json:"deadline,omitempty"`
	Parameters map[string]any `json:"parameters,omitempty"`
	Cluster    string         `json:"cluster,omitempty"`
}

// ComponentStatus is the observed half of a component: what is running.
type ComponentStatus struct {
	ObservedVersion *string    `json:"observed_version"`
	RolloutPhase    string     `json:"rollout_phase"`
	Healthy         bool       `json:"healthy"`
	Message         *string    `json:"message"`
	ObservedAt      *time.Time `json:"observed_at"`
}

// Component pairs a declared version with an observed one. A component nobody
// has reported on counts as behind: silence is not a finished rollout.
type Component struct {
	ID             string          `json:"id"`
	ClusterID      string          `json:"cluster_id"`
	Name           string          `json:"name"`
	Kind           string          `json:"kind"`
	DesiredVersion string          `json:"desired_version"`
	Config         map[string]any  `json:"config"`
	Enabled        bool            `json:"enabled"`
	CreatedAt      time.Time       `json:"created_at"`
	UpdatedAt      *time.Time      `json:"updated_at"`
	Revision       string          `json:"revision"`
	Status         ComponentStatus `json:"status"`
	Behind         bool            `json:"behind"`
}

// EventSubject is what an event is about. `id` is the subject's row
// identifier and is deliberately not a prefixed public id.
type EventSubject struct {
	Type string  `json:"type"`
	ID   *string `json:"id"`
}

// Event is one observed, append-only state record. No declared half, no
// revision: nothing declares an event and nothing may edit one. `ID` is an
// `evt_` public identifier, not the row's primary key.
type Event struct {
	ID          string         `json:"id"`
	ClusterID   *string        `json:"cluster_id"`
	Kind        string         `json:"kind"`
	Severity    string         `json:"severity"`
	Subject     EventSubject   `json:"subject"`
	OperationID *string        `json:"operation_id"`
	Message     string         `json:"message"`
	Details     map[string]any `json:"details"`
	OccurredAt  time.Time      `json:"occurred_at"`
	RecordedAt  time.Time      `json:"recorded_at"`
}
