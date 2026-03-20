// Package observe owns the operator's upward loop: it watches the cluster's own resources,
// projects them into the contract's observed types, and pushes them at the hub through the sync
// client.
//
// The split from internal/reconcile is the declared/observed split made structural. Nothing here
// writes a Kubernetes object and nothing here reads desired state; the only thing this package
// produces is `*.observed` and `*.event` frames, which are full objects and never patches.
//
// Nothing measured travels this way. Utilization, temperature, power draw, throughput and token
// counts are telemetry and go to the observability stack directly (PROTOCOL §7); this loop carries
// membership, readiness, health, ECC counters, throttle state, MIG partitioning and allocation.
package observe

import (
	"context"
	"sort"
	"time"
)

// Condition is one observed condition on a node, already normalised to the wire's vocabulary.
type Condition struct {
	Type    string
	Status  string
	Reason  string
	Message string
	Since   time.Time
}

// Ecc is a device's ECC configuration and error counters. Counters are health, not telemetry:
// they are monotone evidence of hardware degrading, not a sampled measurement.
type Ecc struct {
	ModeEnabled          bool
	VolatileCorrected    int
	VolatileUncorrected  int
	AggregateUncorrected int
}

// Mig is a device's MIG partitioning state.
type Mig struct {
	Enabled    bool
	Profile    string
	InstanceID string
}

// Allocation is what a device is currently committed to, as the cluster sees it.
type Allocation struct {
	ReplicaID    string
	DeploymentID string
	ProjectID    string
}

// Device health values.
const (
	HealthHealthy   = "healthy"
	HealthUnhealthy = "unhealthy"
	HealthUnknown   = "unknown"
)

// Device is one accelerator as discovered on a node.
type Device struct {
	UUID            string
	Index           int
	Model           string
	DeviceClass     string
	MemoryTotalMB   int64
	Health          string
	Ecc             Ecc
	Throttled       bool
	ThrottleReasons []string
	Mig             Mig
	// DomainKind and DomainKey name the topology domain this device shares with others —
	// an NVLink domain, a PCIe root, a NUMA node. Empty means the device is in no domain.
	DomainKind string
	DomainKey  string
	Allocation *Allocation
}

// Node is one machine as discovered, together with the devices attached to it.
type Node struct {
	Name           string
	Pool           string
	Ready          bool
	Cordoned       bool
	Conditions     []Condition
	KernelVersion  string
	DriverVersion  string
	RuntimeVersion string
	Labels         map[string]string
	Devices        []Device
	// AllocatedDevices is how many of this node's accelerators are committed to work, counted
	// from the cluster's own accounting rather than asked of the hardware.
	AllocatedDevices int
}

// Workload is one workload found already running in the cluster that the plane did not put there.
//
// It is recorded as a sighting and never as a deployment. The plane has declared nothing about
// this object, so there is no declared-versus-running difference for anything to reconcile — which
// is the point: adopting a model that is already serving must not move a byte of it out of VRAM.
type Workload struct {
	Namespace         string
	Name              string
	Kind              string
	Images            []string
	ReplicasDesired   int
	ReplicasReady     int
	DevicesPerReplica int
	DevicesTotal      int
	Nodes             []string
	Labels            map[string]string
	// Model is what the workload says it is serving, when it says so at all. A hint for the
	// human deciding whether to track it, never something placement reads.
	Model string
}

// Snapshot is one complete reading of the cluster's supply. It is always whole: a partial
// snapshot would be indistinguishable from hardware that has gone away, and the observed stream
// prunes against what it is told.
type Snapshot struct {
	Nodes     []Node
	Workloads []Workload
	At        time.Time
}

// Discoverer produces a Snapshot. The interface exists so the loop can be driven without a
// cluster, and without a GPU: the real implementation reads Kubernetes, the fake reads a struct.
type Discoverer interface {
	Discover(ctx context.Context) (Snapshot, error)
}

// normalise puts a snapshot into a canonical order so that two readings of an unchanged cluster
// compare equal, which is what keeps an idle cluster off the wire.
func (s *Snapshot) normalise() {
	sort.Slice(s.Nodes, func(i, j int) bool { return s.Nodes[i].Name < s.Nodes[j].Name })
	for i := range s.Nodes {
		n := &s.Nodes[i]
		sort.Slice(n.Devices, func(a, b int) bool {
			if n.Devices[a].Index != n.Devices[b].Index {
				return n.Devices[a].Index < n.Devices[b].Index
			}
			return n.Devices[a].UUID < n.Devices[b].UUID
		})
		sort.Slice(n.Conditions, func(a, b int) bool { return n.Conditions[a].Type < n.Conditions[b].Type })
	}
	sort.Slice(s.Workloads, func(i, j int) bool {
		if s.Workloads[i].Namespace != s.Workloads[j].Namespace {
			return s.Workloads[i].Namespace < s.Workloads[j].Namespace
		}
		if s.Workloads[i].Kind != s.Workloads[j].Kind {
			return s.Workloads[i].Kind < s.Workloads[j].Kind
		}
		return s.Workloads[i].Name < s.Workloads[j].Name
	})
	for i := range s.Workloads {
		sort.Strings(s.Workloads[i].Nodes)
		sort.Strings(s.Workloads[i].Images)
	}
	if s.At.IsZero() {
		s.At = time.Now().UTC()
	}
}

// Fake is a Discoverer backed by a snapshot held in memory, for tests and for running the loop
// against a cluster that has no accelerators in it.
type Fake struct {
	snapshot Snapshot
	err      error
	mu       chan struct{}
}

// NewFake builds a Fake serving the given snapshot.
func NewFake(s Snapshot) *Fake {
	f := &Fake{mu: make(chan struct{}, 1)}
	f.mu <- struct{}{}
	f.Set(s)
	return f
}

// Set replaces the snapshot the Fake serves.
func (f *Fake) Set(s Snapshot) {
	<-f.mu
	f.snapshot, f.err = s, nil
	f.mu <- struct{}{}
}

// SetError makes the next Discover fail.
func (f *Fake) SetError(err error) {
	<-f.mu
	f.err = err
	f.mu <- struct{}{}
}

// Discover implements Discoverer.
func (f *Fake) Discover(context.Context) (Snapshot, error) {
	<-f.mu
	s, err := f.snapshot, f.err
	f.mu <- struct{}{}
	if err != nil {
		return Snapshot{}, err
	}
	return s, nil
}
