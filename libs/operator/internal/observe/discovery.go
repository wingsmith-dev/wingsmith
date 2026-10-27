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
	// DomainKind and DomainKey name the topology domain this device shares with others:
	// an NVLink domain, a PCIe root, a NUMA node. Empty means the device is in no domain.
	DomainKind string
	DomainKey  string
	Allocation *Allocation
}

// Taint is one scheduling repellent on a node.
type Taint struct {
	Key    string
	Value  string
	Effect string
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
	// InstanceType is the machine's shape as its provider names it: `g5.12xlarge`, `a2-highgpu-8g`.
	// It is the answer to "what kind of node is this", which node counts alone never give.
	InstanceType string
	// Cloud is the provider this machine belongs to, derived from its provider reference rather
	// than from a label, because the reference is set by the cloud controller and a label is set
	// by whoever last edited it.
	Cloud     string
	Region    string
	Zone      string
	CPUCores  int
	MemoryMiB int64
	CPUArch   string
	OSImage   string
	Taints    []Taint
	Labels    map[string]string
	Devices   []Device
	// Distribution is the Kubernetes flavour this machine runs, resolved at discovery because
	// the labels that identify it are raw cluster labels and never survive into Labels, which is
	// filtered down to what the plane is meant to see.
	//
	// Per node, not per cluster for the same reason Cloud is: it is read off the machine,
	// and a cluster mid-migration genuinely has two answers. The cluster-wide value is the
	// plurality of these.
	Distribution string
	// AllocatedDevices is how many of this node's accelerators are committed to work, counted
	// from the cluster's own accounting, not asked of the hardware.
	AllocatedDevices int
}

// Workload is one workload found already running in the cluster that the plane did not put there.
//
// It is recorded as a sighting and never as a deployment. The plane has declared nothing about
// this object, so there is no declared-versus-running difference for anything to reconcile, which
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
	// Fingerprint digests the pod template: what is running, not how much of it. It is what
	// an adoption is pinned to: the plane records the digest it adopted, and a reconciler that
	// finds a different one refuses to converge instead of restarting the workload to match.
	Fingerprint string
	// Spec is how the workload was told to serve: the image, the arguments, the resources it
	// asked for. Reported verbatim and uninterpreted: reading `--tensor-parallel-size` as
	// parallelism is the plane's job, not this one's, so that improving the reading is a plane
	// deploy instead of a roll across every cluster in the fleet.
	Spec *WorkloadSpec
}

// WorkloadSpec is what one container was told to do, as its own cluster records it.
//
// Environment variables appear here by name and never by value. A serving pod's environment holds
// registry credentials and HuggingFace tokens, `envFrom` a secret is the ordinary way to run one,
// and the control plane has no business storing them. The names are enough to tell a reader
// what the workload expects to find.
type WorkloadSpec struct {
	Image        string
	Command      []string
	Args         []string
	EnvNames     []string
	Requests     map[string]string
	Limits       map[string]string
	NodeSelector map[string]string
}

// Snapshot is one complete reading of the cluster's supply. It is always whole: a partial
// snapshot would be indistinguishable from hardware that has gone away, and the observed stream
// prunes against what it is told.
type Snapshot struct {
	Nodes     []Node
	Workloads []Workload

	// Devices is which pod holds which accelerator, from the same scrape the nodes were built
	// from. Carried on the snapshot, not fetched again by whoever needs it, for the
	// reason every other pairing in this file is read once: two scrapes a moment apart let a
	// card's owner and the workload holding it disagree.
	Devices PodDevices

	// Multinode says a leader-worker backend is installed, so Dynamo can render a replica that
	// spans machines. Discovered, not configured: whether the CRDs are present is a fact
	// about this cluster that the operator can read directly, and a flag asserting it is a
	// second copy of the same truth that can be wrong.
	Multinode bool

	At time.Time
}

// Fleet is what a snapshot says about the cluster as a whole, not about any one machine.
//
// Derived from the nodes, not discovered separately, because every source for it is a node
// attribute: the cloud is on each machine's provider reference and the distribution is stamped in
// each kubelet's version. Taking the majority answer means a cluster keeps reporting sensibly
// while a single odd node joins or a migration is half done.
type Fleet struct {
	Distribution string
	Cloud        string
	Region       string
}

// fleet reduces a snapshot's nodes to the cluster-wide answer, by plurality.
//
// A tie breaks toward the lexicographically smaller value, so that two equally popular answers
// settle on one instead of flapping between them and putting a frame on the wire each time.
func (s *Snapshot) fleet() Fleet {
	distributions, clouds, regions := map[string]int{}, map[string]int{}, map[string]int{}
	for i := range s.Nodes {
		n := &s.Nodes[i]
		if n.Distribution != "" {
			distributions[n.Distribution]++
		}
		if n.Cloud != "" {
			clouds[n.Cloud]++
		}
		if n.Region != "" {
			regions[n.Region]++
		}
	}
	return Fleet{
		Distribution: plurality(distributions),
		Cloud:        plurality(clouds),
		Region:       plurality(regions),
	}
}

// plurality returns the most common key, or empty when there is nothing to count.
func plurality(counts map[string]int) string {
	best, bestCount := "", 0
	for value, count := range counts {
		if count > bestCount || (count == bestCount && value < best) {
			best, bestCount = value, count
		}
	}
	return best
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
