package api

import (
	"encoding/json"
	"time"
)

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
	Architecture   string
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
	Condition      string
	TopologyDomain string
	Target         string
	Deployment     string
	Workload       string
	Harness        string
	Actor          string
	Subject        string
	Severity       string
	Device         string
	Replica        string
	Namespace      string
	Status         string
	Endpoint       string
	Rule           string
	Request        string
	Scope          string
	ServingDigest  string

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
	Present          *bool
	Enforced         *bool
	FleetDefault     *bool

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
	// Distribution, Cloud and ObservedRegion are what the cluster turned out to be, beside the
	// region it was enrolled as. The two disagreeing is a misconfiguration worth seeing.
	Distribution   *string `json:"distribution"`
	Cloud          *string `json:"cloud"`
	ObservedRegion *string `json:"observed_region"`
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

	// MetricsLabel is the `cluster` label this cluster's series carry in the
	// shared metrics store. Nil means it is not observable and its panels say so
	// rather than querying under a label nobody has confirmed.
	MetricsLabel *string `json:"metrics_label"`
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
	InstanceType  *string          `json:"instance_type"`
	Cloud         *string          `json:"cloud"`
	Zone          *string          `json:"zone"`
	Region        *string          `json:"region"`
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
	Nodes             []string       `json:"nodes"`
	NodeNames         []string       `json:"node_names"`
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

// DeviceClass is one GPU model as the plane names it: a dictionary entry, not a declared or an
// observed object. Class is the identifier and the only string that goes on the wire; Label is the
// only one meant for a person.
//
// InCatalog false means the fleet holds hardware the contract table does not name — it resolved to
// the slug of its vendor string and is being scheduled onto anyway. That is the row to act on.
type DeviceClass struct {
	Class        string               `json:"name"`
	Label        string               `json:"label"`
	Architecture string               `json:"architecture"`
	Vendor       string               `json:"vendor"`
	MemoryMiB    int                  `json:"memory_mib"`
	FormFactor   string               `json:"form_factor"`
	InCatalog    bool                 `json:"in_catalog"`
	Aliases      map[string][]string  `json:"aliases"`
	Inventory    DeviceClassInventory `json:"inventory"`
}

// DeviceClassInventory is what the fleet holds of one class, counted on read from observed state.
type DeviceClassInventory struct {
	Devices     int      `json:"devices"`
	Healthy     int      `json:"healthy"`
	Allocatable int      `json:"allocatable"`
	Allocated   int      `json:"allocated"`
	Nodes       int      `json:"nodes"`
	Clusters    []string `json:"clusters"`
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
	ID     string `json:"id"`
	Digest string `json:"digest"`
	// Verified is false for weights adopted by reference, where the digest is a
	// name and nothing has hashed anything.
	Verified       bool               `json:"verified"`
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

// ServingParallelism is the tensor/pipeline/expert split.
type ServingParallelism struct {
	Tensor   int `json:"tensor"`
	Pipeline int `json:"pipeline"`
	Expert   int `json:"expert"`
}

// ServingCache is KV and prefix cache configuration, unrelated to the cluster
// weight cache.
type ServingCache struct {
	KVCacheDtype       *string        `json:"kv_cache_dtype"`
	KVCacheFraction    *float64       `json:"kv_cache_fraction"`
	PrefixCacheEnabled bool           `json:"prefix_cache_enabled"`
	PrefixCacheConfig  map[string]any `json:"prefix_cache_config"`
}

// ServingShape is the hardware a serving configuration implies, class first: a
// count without a class is not a shape.
//
// The topology pair is here rather than beside the scale numbers because it is a
// property of one replica's layout, the same way the counts are.
type ServingShape struct {
	DeviceClass                  string  `json:"device_class"`
	DeviceCount                  int     `json:"device_count"`
	DeviceMemoryMiBMin           int     `json:"device_memory_mib_min"`
	RequiresSingleTopologyDomain bool    `json:"requires_single_topology_domain"`
	TopologyDomainKind           *string `json:"topology_domain_kind"`
}

// ServingResources is the non-accelerator footprint.
type ServingResources struct {
	CPUCores  *int `json:"cpu_cores"`
	MemoryMiB *int `json:"memory_mib"`
	DiskGiB   *int `json:"disk_gib"`
}

// ServingReasoning is how a model thinks, as flags the engine is told. What it
// *can* do is ServingCapabilities; this is what it was asked to do.
type ServingReasoning struct {
	DefaultOn                bool    `json:"default_on"`
	Parser                   *string `json:"parser"`
	Transport                string  `json:"transport"`
	ThinkBeginToken          *string `json:"think_begin_token"`
	ThinkEndToken            *string `json:"think_end_token"`
	SuppressEOSUntilThinkEnd bool    `json:"suppress_eos_until_think_end"`
}

// ServingBehaviour is what the served API does, as the engine will actually be
// told to behave. Every field renders to a flag, which is why it lives beside
// `cache` rather than on the endpoint: honouring any of it means a replica
// restart.
//
// It is the block the plane stores under `serving`, and the wire name is kept —
// the Go name is not `Serving` only because it is reached through a field of
// that name already.
type ServingBehaviour struct {
	Reasoning               ServingReasoning `json:"reasoning"`
	StreamUsage             string           `json:"stream_usage"`
	ContextOverflow         string           `json:"context_overflow"`
	StructuredOutputBackend *string          `json:"structured_output_backend"`
	ToolCallParser          *string          `json:"tool_call_parser"`
	MaxImagesPerRequest     *int             `json:"max_images_per_request"`
	MaxVideosPerRequest     *int             `json:"max_videos_per_request"`
	// OutputModality is what the engine emits: `image` or `video` on the omni
	// engine, nil (text) on every other.
	OutputModality *string `json:"output_modality,omitempty"`
	// Diffusion is the omni worker's stage configuration, nil for omni's own
	// defaults.
	Diffusion *ServingDiffusion `json:"diffusion,omitempty"`
}

// ServingDiffusion is what a vLLM-Omni worker reads from its stage-config file:
// the pipeline class and the per-model sampling defaults. The operator writes
// the rest of the file.
type ServingDiffusion struct {
	ModelClassName        *string        `json:"model_class_name,omitempty"`
	DefaultSamplingParams map[string]any `json:"default_sampling_params,omitempty"`
}

// DefaultServingBehaviour is the block the plane assumes when a request sends
// none. A request that sends one sends all of it, so it starts from these.
func DefaultServingBehaviour() ServingBehaviour {
	return ServingBehaviour{
		Reasoning:       ServingReasoning{Transport: "reasoning_content"},
		StreamUsage:     "final_chunk",
		ContextOverflow: "reject",
	}
}

// ServingCapabilities is what this engine × artifact is claimed to be able to
// do. Asserted, never rendered: nothing here becomes a flag or reaches a
// cluster, and a block that is internally consistent and entirely false is
// accepted, stored, and falsified by the first conformance run.
type ServingCapabilities struct {
	ReasoningSupported    bool       `json:"reasoning_supported"`
	StreamingSupported    bool       `json:"streaming_supported"`
	StructuredOutputModes []string   `json:"structured_output_modes"`
	ToolCallingSupported  bool       `json:"tool_calling_supported"`
	ToolChoiceModes       []string   `json:"tool_choice_modes"`
	ParallelToolCalls     bool       `json:"parallel_tool_calls"`
	InputModalities       []string   `json:"input_modalities"`
	PromptCacheMode       string     `json:"prompt_cache_mode"`
	PromptCacheTTLSeconds *int       `json:"prompt_cache_ttl_seconds"`
	AssertedBy            *string    `json:"asserted_by"`
	AssertedAt            *time.Time `json:"asserted_at"`
	SourceURI             *string    `json:"source_uri"`
}

// ServingOverlay is passthrough for what the plane deliberately does not model.
type ServingOverlay struct {
	PodSpec map[string]any `json:"pod_spec"`
	Dynamo  map[string]any `json:"dynamo"`
}

// ServingSpec is the whole reproducible way one artifact is served: engine,
// parallelism, cache configuration and the hardware shape they imply.
//
// It is not an object. It has no name, no version and no id — a workload
// carries exactly one of these and `serving_digest` is its identity, so two
// workloads configured the same way digest the same and nothing has to be
// matched by hand.
type ServingSpec struct {
	ArtifactID           string         `json:"artifact_id"`
	Engine               string         `json:"engine"`
	EngineRuntimeVersion string         `json:"engine_runtime_version"`
	EngineRuntimeImage   *string        `json:"engine_runtime_image"`
	FrontendImage        *string        `json:"frontend_image"`
	EngineArgs           map[string]any `json:"engine_args"`
	// ImportedArgs is the argv a sighting was observed running with. Recorded in
	// full, never rendered: declaring a flag is EngineArgs and reading one is
	// this.
	ImportedArgs   map[string]any     `json:"imported_args"`
	Quantization   *string            `json:"quantization"`
	Dtype          *string            `json:"dtype"`
	Parallelism    ServingParallelism `json:"parallelism"`
	ContextLength  int                `json:"context_length"`
	MaxBatchTokens *int               `json:"max_batch_tokens"`
	Cache          ServingCache       `json:"cache"`
	// Behaviour carries the wire name `serving` inside a block already called
	// `serving`. That is deliberate: the outer block is the whole configuration
	// and the inner one is the typed API-behaviour group that lands in the
	// `serving` column, and a third name here would leave the body, the column
	// and the digest field list each calling it something else.
	Behaviour    ServingBehaviour    `json:"serving"`
	Capabilities ServingCapabilities `json:"capabilities"`
	// ResolvedEngineArgs is derived on every read and stored nowhere: the
	// declared engine args with the flags Behaviour dictates laid over them,
	// keyed the way the operator keys them.
	ResolvedEngineArgs map[string]any   `json:"resolved_engine_args"`
	Shape              ServingShape     `json:"shape"`
	Resources          ServingResources `json:"resources"`
	Overlay            ServingOverlay   `json:"overlay"`
}

// ServingRequest is the serving configuration a workload is created with.
//
// Shaped like the block a read returns, minus the derivations the plane owns:
// there is no digest here and no resolved args, because neither is ever accepted
// from a client.
type ServingRequest struct {
	Artifact             string             `json:"artifact"`
	Engine               string             `json:"engine"`
	EngineRuntimeVersion string             `json:"engine_runtime_version"`
	ContextLength        int                `json:"context_length,omitempty"`
	Shape                ServingShape       `json:"shape"`
	EngineRuntimeImage   *string            `json:"engine_runtime_image,omitempty"`
	FrontendImage        *string            `json:"frontend_image,omitempty"`
	EngineArgs           map[string]any     `json:"engine_args,omitempty"`
	ImportedArgs         map[string]any     `json:"imported_args,omitempty"`
	Quantization         *string            `json:"quantization,omitempty"`
	Dtype                *string            `json:"dtype,omitempty"`
	Parallelism          ServingParallelism `json:"parallelism"`
	MaxBatchTokens       *int               `json:"max_batch_tokens,omitempty"`
	Cache                ServingCache       `json:"cache"`
	Resources            ServingResources   `json:"resources"`
	Overlay              ServingOverlay     `json:"overlay"`
	// Behaviour and Capabilities are nil unless a flag set something in them, so
	// the plane's own defaults stand. Sent as zero values they were refused: an
	// empty transport and null modality lists are not the defaults, they are
	// invalid.
	Behaviour    *ServingBehaviour    `json:"serving,omitempty"`
	Capabilities *ServingCapabilities `json:"capabilities,omitempty"`
}

// WorkloadScale is the declared total and what the shares actually add up to.
//
// Unit is named by kind — replicas for service, ranks for gang, completions for
// task — because the three numbers count different things and a bare integer is
// unreadable without it. Placed is the sum of the deployments' shares and Ready
// the sum of their observed ready counts; the plane derives both, and summing a
// share against a total anywhere else is always a bug.
type WorkloadScale struct {
	Unit    string `json:"unit"`
	Min     int    `json:"min"`
	Max     int    `json:"max"`
	Desired int    `json:"desired"`
	Placed  int    `json:"placed"`
	Ready   int    `json:"ready"`
}

// WorkloadDeployment is one cluster's share of a workload, as seen from the
// workload. Enough to draw the per-cluster table; the whole per-cluster object
// is `hgr deployment get`.
type WorkloadDeployment struct {
	ID              string                  `json:"id"`
	Name            string                  `json:"name"`
	ClusterID       string                  `json:"cluster_id"`
	ClusterName     string                  `json:"cluster_name"`
	Region          *string                 `json:"region"`
	MinReplicas     int                     `json:"min_replicas"`
	MaxReplicas     int                     `json:"max_replicas"`
	DesiredReplicas int                     `json:"desired_replicas"`
	AdmissionPhase  string                  `json:"admission_phase"`
	AdmissionReason *string                 `json:"admission_reason"`
	Generation      int                     `json:"generation"`
	Enabled         bool                    `json:"enabled"`
	Authority       string                  `json:"authority"`
	QueuedAt        *time.Time              `json:"queued_at"`
	AdmittedAt      *time.Time              `json:"admitted_at"`
	PlacedAt        *time.Time              `json:"placed_at"`
	Replicas        DeploymentReplicaRollup `json:"replicas"`
	Status          *DeploymentStatus       `json:"status"`
}

// Workload is what you asked to run, globally: an artifact, the way to serve it,
// a kind, a priority and a total scale across every cluster it may use.
//
// It carries no cluster. Placement expands it into one Deployment per cluster,
// and each of those carries that cluster's share of the total — which is why
// Scale is the only place in this package where anything is summed.
//
// Kind is service | task | gang, and it is also what makes a workload
// splittable: service and task fan out, a gang is atomic and always has exactly
// one deployment. Phase is derived from the deployments and nothing writes it.
type Workload struct {
	ID            string `json:"id"`
	Name          string `json:"name"`
	Kind          string `json:"kind"`
	Phase         string `json:"phase"`
	ProjectID     string `json:"project_id"`
	PriorityClass string `json:"priority_class"`
	Preemptible   bool   `json:"preemptible"`
	Paused        bool   `json:"paused"`
	GangSize      int    `json:"gang_size"`
	// ServingDigest is derived from Serving on every write and is never accepted
	// from a client. It is what a benchmark run and a wire frame both name, so
	// "the same configuration" is one string rather than a comparison of eight
	// fields.
	ServingDigest      string               `json:"serving_digest"`
	Serving            ServingSpec          `json:"serving"`
	AdapterArtifactIDs []string             `json:"adapter_artifact_ids"`
	Overrides          map[string]any       `json:"overrides"`
	Labels             map[string]any       `json:"labels"`
	Strategy           string               `json:"strategy"`
	ClusterFloors      map[string]any       `json:"cluster_floors"`
	MaxClusters        *int                 `json:"max_clusters"`
	PinnedClusterID    *string              `json:"pinned_cluster_id"`
	Scale              WorkloadScale        `json:"scale"`
	StartedAt          *time.Time           `json:"started_at"`
	FinishedAt         *time.Time           `json:"finished_at"`
	DeadlineAt         *time.Time           `json:"deadline_at"`
	CreatedAt          time.Time            `json:"created_at"`
	UpdatedAt          *time.Time           `json:"updated_at"`
	Revision           string               `json:"revision"`
	Deployments        []WorkloadDeployment `json:"deployments"`
}

// WorkloadCreateRequest is what it takes to run something, without saying where.
//
// Cluster is an override rather than the normal path: absent, placement picks
// from the serving shape, the project's quota, the policies on the workload and
// what the clusters report free. Naming one is a deliberate escape from the
// scheduler — it still has to exist, but it is never checked for capacity, and a
// named cluster forces a single deployment.
//
// The three scale numbers are TOTALS across every cluster this workload may use,
// counted in replicas for service, ranks for gang, completions for task.
type WorkloadCreateRequest struct {
	// Serving is the whole of what to run, stated rather than named: there is no
	// shared serving object to point at, and two workloads asking for the same
	// thing land on the same serving_digest.
	Serving          ServingRequest `json:"serving"`
	Name             string         `json:"name"`
	Project          string         `json:"project"`
	Cluster          string         `json:"cluster,omitempty"`
	PriorityClass    string         `json:"priority_class,omitempty"`
	Kind             string         `json:"kind,omitempty"`
	MinScale         int            `json:"min_scale"`
	MaxScale         int            `json:"max_scale"`
	DesiredScale     int            `json:"desired_scale"`
	Strategy         string         `json:"strategy,omitempty"`
	ClusterFloors    map[string]int `json:"cluster_floors,omitempty"`
	MaxClusters      *int           `json:"max_clusters,omitempty"`
	Enabled          bool           `json:"enabled"`
	Preemptible      bool           `json:"preemptible"`
	GangSize         int            `json:"gang_size,omitempty"`
	DeadlineAt       *time.Time     `json:"deadline_at,omitempty"`
	AdapterArtifacts []string       `json:"adapter_artifacts,omitempty"`
	Overrides        map[string]any `json:"overrides,omitempty"`
	Labels           map[string]any `json:"labels,omitempty"`
}

// WorkloadUpdateRequest is a partial update: an absent field is untouched.
//
// Serving and cluster are absent by design — a deployment is a workload's
// serving configuration on one cluster, so changing either is a new deployment,
// which is cheap because the endpoint in front of it is the stable name. Scale
// is absent because changing it is
// long-running and can sit pending on quota forever, so it is the workload.scale
// operation rather than a field write.
type WorkloadUpdateRequest struct {
	Name             *string         `json:"name,omitempty"`
	AdapterArtifacts *[]string       `json:"adapter_artifacts,omitempty"`
	Overrides        *map[string]any `json:"overrides,omitempty"`
	Labels           *map[string]any `json:"labels,omitempty"`
	PriorityClass    *string         `json:"priority_class,omitempty"`
	Preemptible      *bool           `json:"preemptible,omitempty"`
	Strategy         *string         `json:"strategy,omitempty"`
	ClusterFloors    *map[string]int `json:"cluster_floors,omitempty"`
	MaxClusters      *int            `json:"max_clusters,omitempty"`
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

// Deployment is one workload's share on one cluster: the unit the operator
// reconciles, and the only object in the model that names a cluster.
//
// MinReplicas, MaxReplicas and DesiredReplicas are THIS CLUSTER'S SHARE of the
// workload's total, never the total. Nothing here sums a share against a total
// or re-derives one from the other.
//
// Serving, adapters, overrides and labels are not on it: they are workload facts,
// read through WorkloadID. Admission is on it, because it is per-cluster — a
// workload can be running in one region and queued on quota in another, and
// there is no single phase true of both.
//
// AdoptedDiscoveredID is the *discovered* workload this deployment was promoted
// from and has nothing to do with WorkloadID; its JSON name is historical.
type Deployment struct {
	ID              string     `json:"id"`
	Name            string     `json:"name"`
	WorkloadID      string     `json:"workload_id"`
	WorkloadName    string     `json:"workload_name"`
	ClusterID       string     `json:"cluster_id"`
	MinReplicas     int        `json:"min_replicas"`
	MaxReplicas     int        `json:"max_replicas"`
	DesiredReplicas int        `json:"desired_replicas"`
	AdmissionPhase  string     `json:"admission_phase"`
	AdmissionReason *string    `json:"admission_reason"`
	QueuedAt        *time.Time `json:"queued_at"`
	AdmittedAt      *time.Time `json:"admitted_at"`
	PlacedAt        *time.Time `json:"placed_at"`
	Generation      int        `json:"generation"`
	Enabled         bool       `json:"enabled"`
	CreatedAt       time.Time  `json:"created_at"`
	UpdatedAt       *time.Time `json:"updated_at"`
	Revision        string     `json:"revision"`

	AdoptedDiscoveredID *string `json:"adopted_workload_id"`
	AdoptedFingerprint  *string `json:"adopted_fingerprint"`
	Authority           string  `json:"authority"`

	Replicas DeploymentReplicaRollup `json:"replicas"`
	Status   *DeploymentStatus       `json:"status"`
}

// DeploymentUpdateRequest is a partial update of one cluster's copy.
//
// Everything a workload declares is absent: it is declared once, globally, and a
// per-cluster override would be a second answer to a question that already has
// one. Scale is absent because shares are derived by placement from the workload
// total, never asserted per cluster.
type DeploymentUpdateRequest struct {
	Enabled *bool `json:"enabled,omitempty"`
}

// DeploymentStatus is what the cluster reports the deployment is doing, and why.
//
// Reason and Message are the sub-state within Phase and are deliberately not errors: a deployment
// waiting for its weights to download is in a normal state that happens to take a while, and the
// only thing worse than not saying so is saying it in a field called last_error. Nil until a
// cluster has reported at all, which is the first seconds of every deployment's life and the
// permanent state of one whose cluster cannot reach the hub.
type DeploymentStatus struct {
	ObservedGeneration  int        `json:"observed_generation"`
	Phase               string     `json:"phase"`
	Reason              *string    `json:"reason"`
	Message             *string    `json:"message"`
	DesiredReplicas     int        `json:"desired_replicas"`
	ReadyReplicas       int        `json:"ready_replicas"`
	UpdatedReplicas     int        `json:"updated_replicas"`
	UnavailableReplicas int        `json:"unavailable_replicas"`
	LastErrorCode       *string    `json:"last_error_code"`
	LastErrorMessage    *string    `json:"last_error_message"`
	LastErrorAt         *time.Time `json:"last_error_at"`
	ObservedAt          *time.Time `json:"observed_at"`
}

// DiscoveredWorkload is something already running in a cluster that the plane
// never declared. Status is `new` until somebody decides: `tracked` puts it in
// the inventory, `ignored` takes it out of the list.
//
// There is no generation and no serving configuration here, and that is the
// point. Adopting one
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

// PromoteRequest turns a sighting into a workload the plane manages.
//
// Fingerprint is the digest the caller was shown. The plane refuses the promotion if the workload
// has changed since, so an adoption is never pinned to a spec nobody looked at. There is no field
// here for the image, the overrides or the replica count: anything that could change what the
// workload is running would make promotion capable of restarting it.
//
// Name names the workload, and the deployment placement pins to the running object takes the same
// name in that one cluster.
type PromoteRequest struct {
	Artifact      string `json:"artifact"`
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
	ID   string `json:"id"`
	Name string `json:"name"`
	// Slug is what a client sends as `model`, and the key the gateway routes on. Name is the
	// internal handle and routes nothing — the two are separate so that a public model id can be
	// corrected without moving the handle every link and metric label holds.
	Slug            string           `json:"slug"`
	ProjectID       string           `json:"project_id"`
	Visibility      string           `json:"visibility"`
	RoutingStrategy string           `json:"routing_strategy"`
	Enabled         bool             `json:"enabled"`
	Targets         []EndpointTarget `json:"targets"`
	FailoverOrder   []string         `json:"failover_order"`
	// Surface is the terms on which the served API is exposed. Declared state: the plane writes
	// it, the gateway reads it off the resolved table, and nothing on a cluster ever sees it.
	Surface   EndpointSurface `json:"surface"`
	Labels    map[string]any  `json:"labels"`
	CreatedAt time.Time       `json:"created_at"`
	UpdatedAt *time.Time      `json:"updated_at"`
	Revision  string          `json:"revision"`
}

// EndpointSurface is what this name exposes: which protocols answer at it, how a caller
// authenticates, the ceilings the gateway applies, and the request policy it enforces.
//
// Every field is optional and an absent one is not a claim — it is the gateway's own behaviour.
// Half of the surface is a term the gateway acts on and half is a disclosure nothing upholds; the
// console draws the difference, and this read-only view states the values without promising which
// is which.
type EndpointSurface struct {
	Protocols            []string        `json:"protocols"`
	AuthMode             string          `json:"auth_mode"`
	ErrorShape           string          `json:"error_shape"`
	StreamingTransport   string          `json:"streaming_transport"`
	OutputLengthParams   []string        `json:"output_length_params"`
	CORSEnabled          *bool           `json:"cors_enabled"`
	MaxRequestBytes      *int            `json:"max_request_bytes"`
	RequestTimeoutSecond *int            `json:"request_timeout_seconds"`
	Policy               *EndpointPolicy `json:"policy"`
}

// EndpointPolicy is the request terms this endpoint sells, on top of whatever the engine does.
//
// Version is what a reader that does not understand a future rule reports rather than guesses. A
// policy stored in the older language is converted by the plane on read, so only one shape arrives
// here.
type EndpointPolicy struct {
	Version int          `json:"version"`
	Rules   []PolicyRule `json:"rules"`
}

// PolicyRule is one term: where it applies, what it is about, and what it says.
//
// Route, Stream and WhenPresent are the scope and an omitted one matches everything. Exactly one
// of Key — a dotted JSON path into the request body — and Predicate — a rule the gateway
// implements in Go — is the subject. The rest are the terms, and Default, Drop and ValueFrom
// rewrite a request where Deny refuses one.
type PolicyRule struct {
	Route       string   `json:"route"`
	Stream      *bool    `json:"stream"`
	WhenPresent string   `json:"when_present"`
	Key         string   `json:"key"`
	Predicate   string   `json:"predicate"`
	Enum        []any    `json:"enum"`
	Min         *float64 `json:"min"`
	Max         *float64 `json:"max"`
	Deny        bool     `json:"deny"`
	Drop        bool     `json:"drop"`
	Default     any      `json:"default"`
	ValueFrom   string   `json:"value_from"`
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

// Tenant is the billing and isolation root a project hangs off. ProjectCount is
// on the read shape because the only destructive question anyone asks of a
// tenant is what deleting it would take with it.
type Tenant struct {
	ID           string     `json:"id"`
	Slug         string     `json:"slug"`
	Name         string     `json:"name"`
	Enabled      bool       `json:"enabled"`
	ProjectCount int        `json:"project_count"`
	CreatedAt    time.Time  `json:"created_at"`
	UpdatedAt    *time.Time `json:"updated_at"`
	Revision     string     `json:"revision"`
}

// Project is the tenancy unit quota and priority attach to.
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

// ServingQuotaStandard is whether a declared commitment clears the acceptance
// standard's floors. Declared against declared: no part of it is a measurement,
// and whether the endpoint then delivers is the gateway's to observe.
//
// An uncommitted field is a shortfall. The standard asks for a floor, and
// declaring nothing is not clearing it.
type ServingQuotaStandard struct {
	Meets      bool     `json:"meets"`
	Shortfalls []string `json:"shortfalls"`
}

// ServingQuota is one endpoint's committed request rate, token rate and
// concurrency. It is not a variant of Quota: a device quota is checked once by
// placement when a workload is admitted, and this is checked on every request
// by the gateway, in front of the clusters. Different table, different
// enforcer, different moment, no field in common.
//
// Every limit is nullable and null means UNCOMMITTED — never zero. There is no
// `usage` here and there deliberately is no observed sibling of `Quota.Usage`:
// a serving quota's usage is measured per request in the gateway and never
// crosses into the control plane.
type ServingQuota struct {
	ID                     string               `json:"id"`
	Endpoint               string               `json:"endpoint"`
	EndpointName           string               `json:"endpoint_name"`
	Project                string               `json:"project"`
	CommittedRPM           *int                 `json:"committed_rpm"`
	CommittedTPM           *int64               `json:"committed_tpm"`
	MaxConcurrentRequests  *int                 `json:"max_concurrent_requests"`
	MaxOutputTokens        *int                 `json:"max_output_tokens"`
	BurstRPM               *int                 `json:"burst_rpm"`
	RampUpSeconds          int                  `json:"ramp_up_seconds"`
	OnExceed               string               `json:"on_exceed"`
	QueueTimeoutMS         *int                 `json:"queue_timeout_ms"`
	LimitScope             string               `json:"limit_scope"`
	SuccessRateObjective   *float64             `json:"success_rate_objective"`
	AvailabilityObjective  string               `json:"availability_objective"`
	ObjectiveWindowSeconds int                  `json:"objective_window_seconds"`
	Enforced               bool                 `json:"enforced"`
	CreatedAt              time.Time            `json:"created_at"`
	UpdatedAt              *time.Time           `json:"updated_at"`
	Revision               string               `json:"revision"`
	Standard               ServingQuotaStandard `json:"standard"`
}

// ResolvedLimits is the traffic ceiling for one endpoint, flattened for a hop
// that holds it in memory. `limit_scope` is the one field that is not a number
// the gateway enforces but an instruction for how to read the numbers beside
// it, and it has to travel with them.
type ResolvedLimits struct {
	CommittedRPM          *int   `json:"committed_rpm"`
	BurstRPM              *int   `json:"burst_rpm"`
	RampUpSeconds         int    `json:"ramp_up_seconds"`
	CommittedTPM          *int64 `json:"committed_tpm"`
	MaxConcurrentRequests *int   `json:"max_concurrent_requests"`
	MaxOutputTokens       *int   `json:"max_output_tokens"`
	OnExceed              string `json:"on_exceed"`
	QueueTimeoutMS        *int   `json:"queue_timeout_ms"`
	LimitScope            string `json:"limit_scope"`
	Enforced              bool   `json:"enforced"`
}

// ResolvedServingQuota is what one endpoint's commitment means for a fleet of a
// stated size. `Declared` is what was sold across the whole gateway fleet;
// `Effective` is what the replica at `ReplicaIndex` enforces once the scope is
// applied. `Workloads` is a lookup convenience and never a key.
type ResolvedServingQuota struct {
	Endpoint               string               `json:"endpoint"`
	EndpointName           string               `json:"endpoint_name"`
	Project                string               `json:"project"`
	Workloads              []string             `json:"workloads"`
	Replicas               int                  `json:"replicas"`
	ReplicaIndex           int                  `json:"replica_index"`
	Declared               ResolvedLimits       `json:"declared"`
	Effective              ResolvedLimits       `json:"effective"`
	StarvedReplicas        int                  `json:"starved_replicas"`
	SuccessRateObjective   *float64             `json:"success_rate_objective"`
	AvailabilityObjective  string               `json:"availability_objective"`
	ObjectiveWindowSeconds int                  `json:"objective_window_seconds"`
	Standard               ServingQuotaStandard `json:"standard"`
	Revision               string               `json:"revision"`
}

// SLAQuery is the period an SLA report covers: a `YYYY-MM` month, a from/to
// range, or neither, which the plane reads as the current calendar month.
type SLAQuery struct {
	Month string
	From  *time.Time
	To    *time.Time
}

// SLAPercentiles is one latency at the three percentiles a contract quotes, in
// seconds. A nil is a percentile nothing measured, never zero.
type SLAPercentiles struct {
	P50 *float64 `json:"p50"`
	P95 *float64 `json:"p95"`
	P99 *float64 `json:"p99"`
}

// SLAObjective is the commitment the endpoint was sold under, quoted from its
// serving quota. SuccessRate is a fraction in (0, 1].
type SLAObjective struct {
	SuccessRate   *float64 `json:"success_rate"`
	WindowSeconds int      `json:"window_seconds"`
	Enforced      bool     `json:"enforced"`
}

// SLAReport is one endpoint's service level over a period, measured at the
// front door. Availability and FailureRatio are percentages on 0-100; Met and
// ErrorBudgetConsumed are nil whenever the reading or the commitment they need
// is missing, because a false that means "not measured" is worse than none.
type SLAReport struct {
	Endpoint            string         `json:"endpoint"`
	EndpointName        string         `json:"endpoint_name"`
	PeriodStart         time.Time      `json:"period_start"`
	PeriodEnd           time.Time      `json:"period_end"`
	Partial             bool           `json:"partial"`
	Definition          string         `json:"definition"`
	DefinitionText      string         `json:"definition_text"`
	Requests            *int64         `json:"requests"`
	Availability        *float64       `json:"availability"`
	FailureRatio        *float64       `json:"failure_ratio"`
	Latency             SLAPercentiles `json:"latency"`
	TTFT                SLAPercentiles `json:"ttft"`
	Objective           *SLAObjective  `json:"objective"`
	Met                 *bool          `json:"met"`
	ErrorBudgetConsumed *float64       `json:"error_budget_consumed"`
	Unavailable         *string        `json:"unavailable"`
}

// ResolvedServingQuotaTable is every enabled endpoint that carries a
// commitment, resolved for one fleet. Unpaginated, like the routing table it
// sits beside.
type ResolvedServingQuotaTable struct {
	Revision     string                 `json:"revision"`
	Replicas     int                    `json:"replicas"`
	ReplicaIndex int                    `json:"replica_index"`
	Quotas       []ResolvedServingQuota `json:"quotas"`
}

// ServingQuotaCreate declares a commitment. Never auto-created: a serving quota
// is a number somebody sold.
type ServingQuotaCreate struct {
	Endpoint               string   `json:"endpoint"`
	CommittedRPM           *int     `json:"committed_rpm,omitempty"`
	CommittedTPM           *int64   `json:"committed_tpm,omitempty"`
	MaxConcurrentRequests  *int     `json:"max_concurrent_requests,omitempty"`
	MaxOutputTokens        *int     `json:"max_output_tokens,omitempty"`
	BurstRPM               *int     `json:"burst_rpm,omitempty"`
	RampUpSeconds          *int     `json:"ramp_up_seconds,omitempty"`
	OnExceed               string   `json:"on_exceed,omitempty"`
	QueueTimeoutMS         *int     `json:"queue_timeout_ms,omitempty"`
	LimitScope             string   `json:"limit_scope,omitempty"`
	SuccessRateObjective   *float64 `json:"success_rate_objective,omitempty"`
	AvailabilityObjective  string   `json:"availability_objective,omitempty"`
	ObjectiveWindowSeconds *int     `json:"objective_window_seconds,omitempty"`
	Enforced               *bool    `json:"enforced,omitempty"`
}

// ServingQuotaUpdate is a partial update: an absent field is untouched. The
// endpoint is absent by design — it is the row's whole scope, and moving one is
// withdrawing a commitment from one contract and granting it to another.
type ServingQuotaUpdate struct {
	CommittedRPM           *int     `json:"committed_rpm,omitempty"`
	CommittedTPM           *int64   `json:"committed_tpm,omitempty"`
	MaxConcurrentRequests  *int     `json:"max_concurrent_requests,omitempty"`
	MaxOutputTokens        *int     `json:"max_output_tokens,omitempty"`
	BurstRPM               *int     `json:"burst_rpm,omitempty"`
	RampUpSeconds          *int     `json:"ramp_up_seconds,omitempty"`
	OnExceed               string   `json:"on_exceed,omitempty"`
	QueueTimeoutMS         *int     `json:"queue_timeout_ms,omitempty"`
	LimitScope             string   `json:"limit_scope,omitempty"`
	SuccessRateObjective   *float64 `json:"success_rate_objective,omitempty"`
	AvailabilityObjective  string   `json:"availability_objective,omitempty"`
	ObjectiveWindowSeconds *int     `json:"objective_window_seconds,omitempty"`
	Enforced               *bool    `json:"enforced,omitempty"`
}

// Serving quota vocabulary, as the plane spells it.
const (
	ScopePerReplica = "per_replica"
	ScopeDivided    = "divided"

	OnExceedReject = "reject"
	OnExceedQueue  = "queue"
)

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

// BenchmarkProvenance is what was running when a run was taken, snapshotted at
// creation. Without it a run is a number with nothing to attribute it to.
type BenchmarkProvenance struct {
	Model                string `json:"model"`
	DeploymentGeneration int64  `json:"deployment_generation"`
	// ServingDigest names the serving configuration the run was taken against.
	// A digest and not an id and a version: the pair it replaces named a
	// mutable object, and a digest cannot drift.
	ServingDigest *string        `json:"serving_digest"`
	EngineArgs    map[string]any `json:"engine_args"`
	// Serving is the workload's declared serving block as it stood when the run
	// was taken. Without it a conformance result cannot be attributed to what
	// the surface claimed to be.
	Serving map[string]any `json:"serving"`
}

// BenchmarkRun is one measurement of one deployment.
//
// Summary is json.RawMessage and not a typed struct. It is the harness's own
// export, stored verbatim by the plane, and giving it a shape here would be this
// client asserting it knows a format nothing promised. `hgr benchmark get -o
// json` hands it straight to jq, which is the tool for a shape you do not know.
type BenchmarkRun struct {
	ID string `json:"id"`
	// RequestID names the ask this run was expanded from. Null for a run taken
	// before requests existed.
	RequestID *string `json:"request_id"`
	// DeploymentID and ClusterID are null for an endpoint-level run: a request
	// to a public name is routed by weight across every enabled upstream, so
	// the run measures whatever the gateway chose.
	DeploymentID   *string        `json:"deployment_id"`
	ClusterID      *string        `json:"cluster_id"`
	EndpointID     *string        `json:"endpoint_id"`
	OperationID    *string        `json:"operation_id"`
	Harness        string         `json:"harness"`
	HarnessVersion *string        `json:"harness_version"`
	Image          string         `json:"image"`
	Parameters     map[string]any `json:"parameters"`
	// StepIndex orders the runs of one sweep or ramp; each one measures a
	// single load point.
	StepIndex int `json:"step_index"`
	// Vantage is where the load was generated from. Observed, and null until
	// something reports it.
	Vantage *string `json:"vantage"`
	// Discard marks a warm-up: a row that can be looked at when the warm-up was
	// itself the thing that failed, and that no verdict rollup counts.
	Discard      bool                `json:"discard"`
	RequestedBy  string              `json:"requested_by"`
	Reason       *string             `json:"reason"`
	Provenance   BenchmarkProvenance `json:"provenance"`
	Phase        string              `json:"phase"`
	Summary      json.RawMessage     `json:"summary"`
	ArtifactsURI *string             `json:"artifacts_uri"`
	Error        *string             `json:"error"`
	CreatedAt    time.Time           `json:"created_at"`
	UpdatedAt    *time.Time          `json:"updated_at"`
	StartedAt    *time.Time          `json:"started_at"`
	FinishedAt   *time.Time          `json:"finished_at"`
}

// Level is which claim this run makes, read off which target it carries. A
// deployment run measures one cluster's configuration and an endpoint run
// measures what a caller actually gets; the two must never be averaged.
func (b BenchmarkRun) Level() string {
	switch {
	case b.EndpointID != nil && *b.EndpointID != "":
		return LevelEndpoint
	case b.DeploymentID != nil && *b.DeploymentID != "":
		return LevelDeployment
	}
	return ""
}

// Target is the id of whatever this run measured, endpoint first: an
// endpoint-level run carries no deployment at all.
func (b BenchmarkRun) Target() string {
	if b.EndpointID != nil && *b.EndpointID != "" {
		return *b.EndpointID
	}
	if b.DeploymentID != nil {
		return *b.DeploymentID
	}
	return ""
}

// Band is the input-length band this run's declared prompt length lands in.
// Empty when no prompt length was declared, which is a coverage gap rather than
// a band of its own.
func (b BenchmarkRun) Band() string {
	value, ok := b.Parameters["input_tokens_mean"]
	if !ok {
		return ""
	}
	f, ok := value.(float64)
	if !ok {
		return ""
	}
	return BandFor(int(f))
}

// Testing levels, as the plane spells them.
const (
	LevelDeployment = "deployment"
	LevelWorkload   = "workload"
	LevelEndpoint   = "endpoint"
)

// Benchmark modes: a shape of runs over one harness, deliberately not a harness
// of its own.
const (
	ModeSingle = "single"
	ModeWarmup = "warmup"
	ModeRamp   = "ramp"
	ModeSweep  = "sweep"
	ModeSoak   = "soak"
)

// Verdict outcomes. `inconclusive` is first-class and is what a missing
// measurement produces; it is never a pass.
const (
	VerdictPass          = "pass"
	VerdictFail          = "fail"
	VerdictInconclusive  = "inconclusive"
	VerdictNotApplicable = "not_applicable"
)

// BandUnknown is a run with no declared prompt length. Its own value rather
// than a silent drop: a coverage gap belongs on the panel, not inside the
// number.
const BandUnknown = "unknown"

// bandUppers are the input-length bands, upper bound inclusive, K being 1024
// the way a context window is quoted. Duplicated from the plane on purpose:
// the boundaries belong to a frozen criteria version, and a shared constant
// would let an edit silently rewrite what an old verdict meant.
var bandUppers = []struct {
	label string
	upper int
}{
	{"lt6k", 6 * 1024},
	{"6k_16k", 16 * 1024},
	{"16k_32k", 32 * 1024},
	{"32k_64k", 64 * 1024},
	{"64k_128k", 128 * 1024},
	{"128k_256k", 256 * 1024},
}

// BandFor names the band a prompt length falls in. A length landing exactly on
// a boundary takes the stricter band, which is always the lower one.
func BandFor(inputTokens int) string {
	for _, b := range bandUppers {
		if inputTokens <= b.upper {
			return b.label
		}
	}
	return "gt256k"
}

// BenchmarkCoverage is one thing that was behind the target when a request
// expanded. Snapshotted at expansion and allowed to go stale loudly: a workload
// rescaled mid-run does not retroactively change what was measured.
type BenchmarkCoverage struct {
	DeploymentID  *string `json:"deployment_id"`
	ClusterID     *string `json:"cluster_id"`
	ServingDigest *string `json:"serving_digest"`
	ArtifactID    *string `json:"artifact_id"`
}

// BenchmarkRequest is the ask: what to measure, at which level, and against
// which frozen standard. It fans out into runs and is their parent.
//
// It carries no summary and no verdict of its own. Its phase is derived from
// its runs and so is its verdict, because a stored verdict cannot answer "would
// this have passed under the new rules".
type BenchmarkRequest struct {
	ID              string              `json:"id"`
	Level           string              `json:"level"`
	TargetID        string              `json:"target_id"`
	Harness         string              `json:"harness"`
	Image           string              `json:"image"`
	Parameters      map[string]any      `json:"parameters"`
	Mode            string              `json:"mode"`
	CriteriaVersion string              `json:"criteria_version"`
	Tier            *string             `json:"tier"`
	Homogeneous     bool                `json:"homogeneous"`
	Coverage        []BenchmarkCoverage `json:"coverage"`
	RequestedBy     string              `json:"requested_by"`
	Reason          *string             `json:"reason"`
	Phase           string              `json:"phase"`
	Runs            []string            `json:"runs"`
	CreatedAt       time.Time           `json:"created_at"`
	UpdatedAt       *time.Time          `json:"updated_at"`
}

// Terminal reports whether every run behind the request has stopped moving.
func (b BenchmarkRequest) Terminal() bool {
	switch b.Phase {
	case OpPending, OpRunning:
		return false
	}
	return true
}

// BenchmarkCheck is one graded assertion, with the number it graded and the
// number it graded against. Both are carried so a reader can see how close a
// pass was, which is the question asked immediately after a pass.
type BenchmarkCheck struct {
	ID         string   `json:"id"`
	Label      string   `json:"label"`
	Outcome    string   `json:"outcome"`
	Observed   *float64 `json:"observed"`
	Threshold  *float64 `json:"threshold"`
	Comparator string   `json:"comparator"`
	Detail     *string  `json:"detail"`
}

// BenchmarkVerdict is whether a run met the standard, derived at read against a
// pinned criteria version and stored nowhere. The version is what makes
// deriving it honest, so it is never shown without one.
type BenchmarkVerdict struct {
	Outcome         string           `json:"outcome"`
	CriteriaVersion string           `json:"criteria_version"`
	Reason          *string          `json:"reason"`
	Checks          []BenchmarkCheck `json:"checks"`
}

// BenchmarkBand is one input-length band of a request, re-aggregated from the
// runs that landed in it. The standard sets a time-to-first-token target per
// band, so a single aggregate over a whole sweep evidences none of them.
//
// P90 and P50 are the worst of the band because percentiles do not average;
// Avg is weighted by request count, which does. Incomplete names the runs that
// reported no time to first token at all.
type BenchmarkBand struct {
	Band       string   `json:"band"`
	Runs       int      `json:"runs"`
	Requests   int      `json:"requests"`
	TTFTP90MS  *float64 `json:"ttft_p90_ms"`
	TTFTP50MS  *float64 `json:"ttft_p50_ms"`
	TTFTAvgMS  *float64 `json:"ttft_avg_ms"`
	Incomplete []string `json:"incomplete"`
}

// Terminal reports whether a run has stopped moving.
func (b BenchmarkRun) Terminal() bool {
	switch b.Phase {
	case OpPending, OpRunning:
		return false
	}
	return true
}

// BenchmarkParameter is one knob a harness accepts, as the plane describes it.
// `Levels` narrows a knob to a subset of the harness's own; empty means every
// level the harness runs at.
type BenchmarkParameter struct {
	Type        string   `json:"type"`
	Name        string   `json:"name"`
	Default     any      `json:"default"`
	Description string   `json:"description"`
	Levels      []string `json:"levels"`
}

// BenchmarkHarness is a probe suite or load generator, the levels it grades at,
// and what it takes. The levels are served rather than assumed, so there is no
// second answer to what a harness may be pointed at.
type BenchmarkHarness struct {
	Name        string               `json:"name"`
	Image       string               `json:"image"`
	Description string               `json:"description"`
	Levels      []string             `json:"levels"`
	Parameters  []BenchmarkParameter `json:"parameters"`
	MaxProbes   *int                 `json:"max_probes"`
}

// RunsAt reports whether this harness has anything to say at a level.
func (h BenchmarkHarness) RunsAt(level string) bool {
	for _, l := range h.Levels {
		if l == level {
			return true
		}
	}
	return false
}

// BenchmarkRunCreate starts one run against one deployment. It is the
// deployment-level shortcut and expands into the same request-and-runs pair a
// levelled request does; there is one creation path, not two.
type BenchmarkRunCreate struct {
	Deployment string         `json:"deployment"`
	Harness    string         `json:"harness"`
	Image      string         `json:"image,omitempty"`
	Reason     string         `json:"reason,omitempty"`
	Parameters map[string]any `json:"parameters,omitempty"`
	Criteria   string         `json:"criteria,omitempty"`
	Tier       string         `json:"tier,omitempty"`
	Deadline   *time.Time     `json:"deadline,omitempty"`
}

// BenchmarkRequestCreate is what to measure and at which level.
//
// `Steps` are the load points a stepped mode walks — concurrencies for a ramp,
// prompt lengths for a sweep. A sweep is what makes a per-band
// time-to-first-token claim evidenceable at all: one invocation measures one
// load point, and the standard sets a target for each input-length band.
type BenchmarkRequestCreate struct {
	Level      string         `json:"level"`
	Target     string         `json:"target"`
	Harness    string         `json:"harness"`
	Image      string         `json:"image,omitempty"`
	Mode       string         `json:"mode,omitempty"`
	Steps      []int          `json:"steps,omitempty"`
	Criteria   string         `json:"criteria,omitempty"`
	Tier       string         `json:"tier,omitempty"`
	Reason     string         `json:"reason,omitempty"`
	Parameters map[string]any `json:"parameters,omitempty"`
	Deadline   *time.Time     `json:"deadline,omitempty"`
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

// AlertSubject is what an alert or a threshold is about, by public id. Unlike
// EventSubject the id is resolvable — and it may name something that no longer
// exists, because an alert about a node that went away has to outlive the node.
//
// A null id on a threshold is the fleet default for that rule.
type AlertSubject struct {
	Type string  `json:"type"`
	ID   *string `json:"id"`
}

// MetricRule is one entry in the catalogue of rules a threshold may name.
// Read-only and code rather than data: the metric and the comparison are
// judgement the plane owns, and the only reason they are on the wire is that a
// client declaring a threshold has to know which number it is being asked for
// and in which unit.
//
// `SuggestedValue` is what the number would be if nobody had said otherwise. It
// is not a default that fires: no metric rule evaluates until a threshold row
// declares one.
type MetricRule struct {
	Name           string  `json:"name"`
	Metric         string  `json:"metric"`
	Unit           string  `json:"unit"`
	Comparison     string  `json:"comparison"`
	SubjectType    string  `json:"subject_type"`
	Severity       string  `json:"severity"`
	Measures       string  `json:"measures"`
	SuggestedValue float64 `json:"suggested_value"`
	Lookback       string  `json:"lookback"`
	ForSeconds     int     `json:"for_seconds"`
}

// AlertThreshold is a number this plane has committed to, and the rule that
// watches it. Declared: a threshold is editable, while the alerts it fires are
// observed and are not.
//
// `Lookback`, `ForSeconds` and `Severity` are returned exactly as declared, null
// included, because null means "whatever the rule says" — substituting the
// rule's own value would make a row look like it had pinned something it
// deliberately left alone.
type AlertThreshold struct {
	ID         string         `json:"id"`
	Rule       string         `json:"rule"`
	Metric     string         `json:"metric"`
	Comparison string         `json:"comparison"`
	Subject    AlertSubject   `json:"subject"`
	Dimensions map[string]any `json:"dimensions"`
	Value      float64        `json:"value"`
	Objective  *string        `json:"objective"`
	Lookback   *string        `json:"lookback"`
	ForSeconds *int           `json:"for_seconds"`
	Severity   *string        `json:"severity"`
	Enabled    bool           `json:"enabled"`
	Reason     *string        `json:"reason"`
	CreatedAt  time.Time      `json:"created_at"`
	UpdatedAt  *time.Time     `json:"updated_at"`
}

// FleetDefault reports whether this row is the one that covers every subject of
// its rule's type, rather than overriding it for one subject.
func (t AlertThreshold) FleetDefault() bool {
	return t.Subject.ID == nil || *t.Subject.ID == ""
}

// AlertThresholdCreate declares a threshold. `comparison` is deliberately
// absent: which way a metric is wrong is not declarable.
type AlertThresholdCreate struct {
	Rule        string         `json:"rule"`
	Value       float64        `json:"value"`
	SubjectType string         `json:"subject_type,omitempty"`
	SubjectID   string         `json:"subject_id,omitempty"`
	Dimensions  map[string]any `json:"dimensions,omitempty"`
	Objective   *string        `json:"objective,omitempty"`
	Lookback    *string        `json:"lookback,omitempty"`
	ForSeconds  *int           `json:"for_seconds,omitempty"`
	Severity    *string        `json:"severity,omitempty"`
	Enabled     *bool          `json:"enabled,omitempty"`
	Reason      string         `json:"reason,omitempty"`
}

// AlertThresholdUpdate is a partial update. `rule`, `subject_type` and
// `subject_id` are not updatable: they are the row's identity, and moving a
// commitment onto a different rule or subject is a different commitment.
type AlertThresholdUpdate struct {
	Value      *float64       `json:"value,omitempty"`
	Dimensions map[string]any `json:"dimensions,omitempty"`
	Objective  *string        `json:"objective,omitempty"`
	Lookback   *string        `json:"lookback,omitempty"`
	ForSeconds *int           `json:"for_seconds,omitempty"`
	Severity   *string        `json:"severity,omitempty"`
	Enabled    *bool          `json:"enabled,omitempty"`
	Reason     *string        `json:"reason,omitempty"`
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
