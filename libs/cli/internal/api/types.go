package api

import "time"

// ListOpts is the common filter/pagination envelope for list calls.
type ListOpts struct {
	All    bool
	Status string
	Region string
	Labels map[string]string
	Search string
	Limit  int
	Cursor string

	// Supply scoping. Cluster, Pool, and Node accept a name, an id, or a
	// unique id prefix, exactly as the matching `get` does.
	Cluster string
	Pool    string
	Node    string
	Class   string
	Health  string
	Domain  string

	// Cordoned narrows a node listing to cordoned members only.
	Cordoned bool
}

// Identity is who the active token belongs to.
type Identity struct {
	User    string `json:"user"`
	Email   string `json:"email,omitempty"`
	Role    string `json:"role,omitempty"`
	Project string `json:"project,omitempty"`
	Server  string `json:"server,omitempty"`
}

// DeviceSummary is the per-cluster accelerator rollup. Device is the
// schedulable unit, so counts are per accelerator and never per node.
type DeviceSummary struct {
	Class     string `json:"class"`
	Total     int    `json:"total"`
	Healthy   int    `json:"healthy"`
	Allocated int    `json:"allocated"`
}

// Cluster is the declared enrolment record joined with the state the
// cluster-resident operator reports back over the sync stream.
type Cluster struct {
	ID           string            `json:"id"`
	Name         string            `json:"name"`
	Region       string            `json:"region"`
	Phase        string            `json:"phase"`
	Reachable    bool              `json:"reachable"`
	AgentVersion string            `json:"agent_version,omitempty"`
	Capabilities []string          `json:"capabilities,omitempty"`
	Labels       map[string]string `json:"labels,omitempty"`
	Nodes        int               `json:"nodes"`
	Devices      []DeviceSummary   `json:"devices,omitempty"`
	Deployments  int               `json:"deployments"`
	EnrolledAt   time.Time         `json:"enrolled_at"`
	LastSyncAt   *time.Time        `json:"last_sync_at,omitempty"`
}

// DeviceTotals sums the per-class rollups.
func (c Cluster) DeviceTotals() DeviceSummary {
	var t DeviceSummary
	for _, d := range c.Devices {
		t.Total += d.Total
		t.Healthy += d.Healthy
		t.Allocated += d.Allocated
	}
	return t
}

// Cluster phases. Enrolment and reachability are different questions, so a
// cluster is never "offline" as a phase — it is active and unreachable.
const (
	PhasePending  = "pending"
	PhaseActive   = "active"
	PhaseDraining = "draining"
	PhaseDisabled = "disabled"
)

// Node is a cluster member as the operator observes it. Cordon state is a
// boolean here and not a status value: draining is an Operation, and the
// cordon is the observed fact it leaves behind.
type Node struct {
	ID         string              `json:"id"`
	Name       string              `json:"name"`
	ClusterID  string              `json:"cluster_id"`
	Cluster    string              `json:"cluster"`
	Status     string              `json:"status"`
	Cordoned   bool                `json:"cordoned"`
	Pool       string              `json:"pool,omitempty"`
	PoolID     string              `json:"pool_id,omitempty"`
	Shape      string              `json:"shape,omitempty"`
	CPUCores   int                 `json:"cpu_cores,omitempty"`
	MemoryGB   int                 `json:"memory_gb,omitempty"`
	Kernel     string              `json:"kernel,omitempty"`
	Driver     string              `json:"driver,omitempty"`
	Devices    []DeviceSummary     `json:"devices,omitempty"`
	Topology   []TopologyDomainRef `json:"topology,omitempty"`
	Conditions []NodeCondition     `json:"conditions,omitempty"`
	Labels     map[string]string   `json:"labels,omitempty"`
	JoinedAt   time.Time           `json:"joined_at"`
	LastSeenAt *time.Time          `json:"last_seen_at,omitempty"`
}

// NodeCondition is one observed condition the operator reports.
type NodeCondition struct {
	Type   string `json:"type"`
	Status string `json:"status"`
	Reason string `json:"reason,omitempty"`
}

// DeviceTotals sums the node's per-class rollups.
func (n Node) DeviceTotals() DeviceSummary {
	var t DeviceSummary
	for _, d := range n.Devices {
		t.Total += d.Total
		t.Healthy += d.Healthy
		t.Allocated += d.Allocated
	}
	return t
}

// TopologyDomainRef is the device-side view of a topology domain: enough to
// answer "are these devices in one fabric" without a second call.
type TopologyDomainRef struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Kind    string `json:"kind"`
	Devices int    `json:"devices,omitempty"`
}

// Device is one accelerator — the schedulable unit. Everything a placement
// decision needs is per-device: health, ECC, throttle, memory, MIG, the
// topology domain it sits in, and what currently holds it.
type Device struct {
	ID              string             `json:"id"`
	UUID            string             `json:"uuid"`
	Index           int                `json:"index"`
	ClusterID       string             `json:"cluster_id"`
	Cluster         string             `json:"cluster"`
	NodeID          string             `json:"node_id"`
	Node            string             `json:"node"`
	Pool            string             `json:"pool,omitempty"`
	Class           string             `json:"class"`
	Model           string             `json:"model,omitempty"`
	MemoryGB        int                `json:"memory_gb"`
	MemoryUsedGB    int                `json:"memory_used_gb"`
	Health          string             `json:"health"`
	HealthReason    string             `json:"health_reason,omitempty"`
	ECCCorrected    int                `json:"ecc_corrected"`
	ECCUncorrected  int                `json:"ecc_uncorrected"`
	Throttled       bool               `json:"throttled"`
	ThrottleReasons []string           `json:"throttle_reasons,omitempty"`
	MIGEnabled      bool               `json:"mig_enabled"`
	MIGProfile      string             `json:"mig_profile,omitempty"`
	Topology        *TopologyDomainRef `json:"topology,omitempty"`
	Allocation      *DeviceAllocation  `json:"allocation,omitempty"`
	Driver          string             `json:"driver,omitempty"`
	LastSeenAt      *time.Time         `json:"last_seen_at,omitempty"`
}

// DeviceAllocation is what currently holds a device. Its absence is the
// entire meaning of "free".
type DeviceAllocation struct {
	Deployment string    `json:"deployment"`
	Replica    string    `json:"replica,omitempty"`
	Project    string    `json:"project,omitempty"`
	Since      time.Time `json:"since"`
}

// Allocated reports whether the device is currently held.
func (d Device) Allocated() bool { return d.Allocation != nil }

// AllocationStatus is the free/allocated axis, kept separate from health
// because an unhealthy device can still be held by a replica.
func (d Device) AllocationStatus() string {
	if d.Allocation != nil {
		return "allocated"
	}
	return "free"
}

// DomainName is the topology domain name, or "-" when the operator reports
// no fabric grouping for the device.
func (d Device) DomainName() string {
	if d.Topology == nil {
		return "-"
	}
	return d.Topology.Name
}

// Pool is a declared provisioning spec — device class, node shape, size
// bounds, and where nodes come from — joined with its observed membership.
type Pool struct {
	ID           string            `json:"id"`
	Name         string            `json:"name"`
	ClusterID    string            `json:"cluster_id"`
	Cluster      string            `json:"cluster"`
	Status       string            `json:"status"`
	DeviceClass  string            `json:"device_class"`
	DevicesPer   int               `json:"devices_per_node,omitempty"`
	Shape        string            `json:"node_shape,omitempty"`
	MinNodes     int               `json:"min_nodes"`
	MaxNodes     int               `json:"max_nodes"`
	Source       string            `json:"provisioning_source"`
	Labels       map[string]string `json:"labels,omitempty"`
	Taints       []string          `json:"taints,omitempty"`
	Nodes        int               `json:"nodes"`
	ReadyNodes   int               `json:"ready_nodes"`
	Devices      DeviceSummary     `json:"devices"`
	TopologyKind string            `json:"topology_kind,omitempty"`
	CreatedAt    time.Time         `json:"created_at"`
	UpdatedAt    *time.Time        `json:"updated_at,omitempty"`
}

// Node statuses, device health, and pool statuses. Node cordon state is
// deliberately absent: it is a boolean, not a status.
const (
	NodeReady    = "ready"
	NodeNotReady = "not_ready"
	NodeUnknown  = "unknown"

	HealthHealthy   = "healthy"
	HealthDegraded  = "degraded"
	HealthUnhealthy = "unhealthy"
	HealthUnknown   = "unknown"

	PoolActive   = "active"
	PoolPending  = "pending"
	PoolPaused   = "paused"
	PoolDisabled = "disabled"

	TopologyNVSwitch = "nvswitch"
	TopologyNVLink   = "nvlink"
	TopologyPCIe     = "pcie"
	TopologyNUMA     = "numa"
)
