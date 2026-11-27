// Package backend is the seam between the operator's protocol side (sync, wire, checkpoint,
// reporting) and whatever actually runs workloads.
//
// The Kubernetes/Dynamo implementation lives in internal/reconcile and internal/observe. A host
// backend (plain engine containers on a Linux machine, P2.3/P2.4) implements the same interface.
// This package must stay free of Kubernetes imports so the host agent can depend on it.
package backend

import (
	"context"
	"fmt"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// Backend applies desired deployments, reads back what is running, and reports supply.
type Backend interface {
	// Apply makes the runtime match d. It returns a *Rejected error for a refusal that will
	// recur unchanged, which the caller reports as failed instead of retrying.
	Apply(ctx context.Context, d Desired) (Applied, error)

	// Prune collects what earlier applies of this deployment left behind now that `current`
	// (the name Apply returned, or the name already running) is what serves it. It runs after
	// every pass that reaches a current workload, written or not.
	Prune(ctx context.Context, dep wire.DeploymentDesired, current string) error

	// Delete removes everything the runtime holds for one deployment id. Absent is success.
	Delete(ctx context.Context, deploymentID string) error

	// Observe reads what is actually running for one deployment, in the rollout vocabulary of
	// MESSAGES.md (phase, closed-set reason, message).
	Observe(ctx context.Context, dep wire.DeploymentDesired) (Rollout, error)

	// Inventory is one complete reading of nodes, devices, topology and capacity.
	Inventory(ctx context.Context) (Inventory, error)
}

// Layout is where one replica's accelerators sit, as decided by the capacity check.
type Layout struct {
	DevicesPerNode  int
	NodesPerReplica int
	Domain          string
}

// Owner identifies the declared object a workload is applied for.
type Owner struct {
	Namespace string
	Name      string
	UID       string
}

// Desired is one deployment ready to apply: every ordering gate has passed.
type Desired struct {
	Deployment wire.DeploymentDesired
	Artifact   wire.ArtifactDesired
	Layout     Layout
	Owner      Owner

	// Hold is called once the desired state has been built and before anything is written, so
	// the caller can record that cleanup is owed. Optional.
	Hold func(context.Context) error
}

// Applied is the result of a successful Apply.
type Applied struct {
	// Name is the runtime's name for the applied workload.
	Name string
	// Unchanged says nothing was written because what runs was built from the same inputs.
	Unchanged bool
}

// Rejected is a refusal that will recur until the declaration changes.
type Rejected struct {
	Reason  string
	Message string
}

func (e *Rejected) Error() string { return fmt.Sprintf("%s: %s", e.Reason, e.Message) }

// Rollout is what is running for one deployment, as opposed to what was applied.
type Rollout struct {
	Phase   string
	Reason  string
	Message string

	Desired     int
	Ready       int
	Updated     int
	Unavailable int

	Replicas    []wire.ReplicaObserved
	Allocations []wire.AllocationObserved
	// Roles is the per-role split, nil for an aggregated deployment.
	Roles     []wire.RoleObserved
	LastError *wire.LastError

	// Converging says the rollout is still filling in and is worth reading again soon.
	Converging bool

	ObservedAt time.Time
}

// Inventory is the supply side: what the machine or cluster has.
type Inventory struct {
	Cluster  wire.ClusterObserved
	Nodes    []wire.NodeObserved
	Devices  []wire.DeviceObserved
	Topology []wire.TopologyObserved
	Capacity []wire.CapacityObserved
	At       time.Time
}
