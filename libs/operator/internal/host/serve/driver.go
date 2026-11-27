package serve

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/backend"
	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// Wire types the driver reads and writes.
const (
	typeDeploymentDesired  = "deployment.desired"
	typeDeploymentDeleted  = "deployment.deleted"
	typeArtifactDesired    = "artifact.desired"
	typeDeploymentObserved = "deployment.observed"
	typeReplicaObserved    = "replica.observed"
	typeAllocationObserved = "allocation.observed"
)

// Reporter sends observed frames to the hub; the sync client implements it.
type Reporter interface {
	Send(t syncpkg.MessageType, subject string, payload any) error
}

// Driver is the host's reconcile loop: it keeps the declared deployments and artifacts in
// memory, drives the Backend toward them and reports what runs. It is the host's sync.Handler.
//
// The declared state is written to StatePath after every change, so a restarted host keeps
// serving what it was last told instead of reading an empty map as "tear everything down" while
// the hub is still unreachable.
type Driver struct {
	Backend   *Backend
	Reporter  Reporter
	ClusterID string
	// Routes receives the ready loopback ports after every pass. Optional.
	Routes   func(map[string][]int)
	Interval time.Duration
	// StatePath is where the declared state is checkpointed. Empty keeps it in memory only.
	StatePath string
	Log       *slog.Logger

	mu        sync.Mutex
	deps      map[string]wire.DeploymentDesired
	artifacts map[string]wire.ArtifactDesired
	// epoch is the set re-declared since a resync began; nil outside one.
	epoch    map[string]bool
	reported map[string]string
	held     map[string]map[string]wire.AllocationObserved
	poke     chan struct{}
}

func (d *Driver) init() {
	if d.deps == nil {
		d.deps = map[string]wire.DeploymentDesired{}
		d.artifacts = map[string]wire.ArtifactDesired{}
		d.reported = map[string]string{}
		d.held = map[string]map[string]wire.AllocationObserved{}
		d.poke = make(chan struct{}, 1)
	}
}

// NewDriver returns a ready driver.
func NewDriver(d *Driver) *Driver {
	d.init()
	if d.Interval <= 0 {
		d.Interval = 10 * time.Second
	}
	if d.Log == nil {
		d.Log = slog.Default()
	}
	return d
}

var _ syncpkg.Handler = (*Driver)(nil)

// Handle implements sync.Handler. It records and returns; the loop does the work, because an
// image pull takes minutes and the session must keep reading.
func (d *Driver) Handle(_ context.Context, e *syncpkg.Envelope) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	switch e.Type {
	case typeDeploymentDesired:
		var p wire.DeploymentDesired
		if err := json.Unmarshal(e.Payload, &p); err != nil || p.DeploymentID == "" {
			d.Log.Warn("undecodable deployment", "error", err)
			return nil
		}
		if old, ok := d.deps[p.DeploymentID]; ok && old.Generation > p.Generation {
			return nil
		}
		d.deps[p.DeploymentID] = p
		if d.epoch != nil {
			d.epoch[p.DeploymentID] = true
		}
	case typeDeploymentDeleted:
		var p wire.DeploymentDeleted
		if err := json.Unmarshal(e.Payload, &p); err != nil {
			return nil
		}
		delete(d.deps, p.DeploymentID)
	case typeArtifactDesired:
		var p wire.ArtifactDesired
		if err := json.Unmarshal(e.Payload, &p); err != nil || p.ArtifactID == "" {
			return nil
		}
		d.artifacts[p.ArtifactID] = p
	case syncpkg.TypeResyncBegin:
		d.epoch = map[string]bool{}
	case syncpkg.TypeStreamEnd:
		var end wire.StreamEnd
		if d.epoch != nil && json.Unmarshal(e.Payload, &end) == nil && end.Complete {
			for id := range d.deps {
				if !d.epoch[id] {
					delete(d.deps, id)
				}
			}
		}
		d.epoch = nil
	default:
		return nil
	}
	d.saveLocked()
	d.wake()
	return nil
}

// checkpoint is the declared state on disk.
type checkpoint struct {
	Deployments map[string]wire.DeploymentDesired `json:"deployments"`
	Artifacts   map[string]wire.ArtifactDesired   `json:"artifacts"`
}

func (d *Driver) saveLocked() {
	if d.StatePath == "" {
		return
	}
	raw, err := json.Marshal(checkpoint{Deployments: d.deps, Artifacts: d.artifacts})
	if err == nil {
		tmp := d.StatePath + ".tmp"
		if err = os.WriteFile(tmp, raw, 0o600); err == nil {
			err = os.Rename(tmp, d.StatePath)
		}
	}
	if err != nil {
		d.Log.Warn("could not checkpoint declared state", "error", err)
	}
}

func (d *Driver) load() error {
	if d.StatePath == "" {
		return nil
	}
	raw, err := os.ReadFile(d.StatePath)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var c checkpoint
	if err := json.Unmarshal(raw, &c); err != nil {
		return fmt.Errorf("declared state %s: %w", d.StatePath, err)
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	for k, v := range c.Deployments {
		d.deps[k] = v
	}
	for k, v := range c.Artifacts {
		d.artifacts[k] = v
	}
	return nil
}

// Resync implements sync.Handler: the snapshot that follows is the whole declared state.
func (d *Driver) Resync(context.Context) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.epoch = map[string]bool{}
	return nil
}

func (d *Driver) wake() {
	select {
	case d.poke <- struct{}{}:
	default:
	}
}

// Run adopts what is already running and reconciles until ctx ends.
func (d *Driver) Run(ctx context.Context) error {
	if err := d.load(); err != nil {
		// Refuse to start rather than read an unreadable checkpoint as "nothing declared",
		// which would tear down every replica on the machine.
		return err
	}
	if err := d.Backend.Adopt(ctx); err != nil {
		d.Log.Warn("could not adopt running containers", "error", err)
	}
	t := time.NewTicker(d.Interval)
	defer t.Stop()
	for {
		d.Pass(ctx)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
		case <-d.poke:
		}
	}
}

// Pass reconciles every declared deployment once and removes what is no longer declared.
func (d *Driver) Pass(ctx context.Context) {
	d.mu.Lock()
	deps := make([]wire.DeploymentDesired, 0, len(d.deps))
	for _, dep := range d.deps {
		deps = append(deps, dep)
	}
	arts := make(map[string]wire.ArtifactDesired, len(d.artifacts))
	for k, v := range d.artifacts {
		arts[k] = v
	}
	resyncing := d.epoch != nil
	d.mu.Unlock()

	declared := map[string]bool{}
	for _, dep := range deps {
		declared[dep.DeploymentID] = true
		d.reconcile(ctx, dep, arts)
	}
	// A deployment withdrawn, or missing from a complete resync, is torn down. Never during a
	// resync: the snapshot is still arriving.
	if !resyncing {
		if managed, err := d.Backend.Managed(ctx); err == nil {
			for id := range managed {
				if id != "" && !declared[id] {
					d.teardown(ctx, id)
				}
			}
		}
	}
	if d.Routes != nil {
		d.Routes(d.Backend.Ready())
	}
}

func (d *Driver) reconcile(ctx context.Context, dep wire.DeploymentDesired, arts map[string]wire.ArtifactDesired) {
	if dep.Lifecycle == "paused" || dep.Authority == "observed" {
		return
	}
	var art wire.ArtifactDesired
	found := false
	for _, id := range dep.ArtifactIDs {
		if a, ok := arts[id]; ok {
			art, found = a, true
			break
		}
	}
	if !found {
		d.pending(dep, "ArtifactNotDeclared", "the artifact this deployment serves has not reached the host")
		return
	}
	applied, err := d.Backend.Apply(ctx, backend.Desired{
		Deployment: dep, Artifact: art,
		Layout: backend.Layout{DevicesPerNode: dep.Placement.DevicesPerReplica, NodesPerReplica: 1},
	})
	var rejected *backend.Rejected
	var pull *pullError
	switch {
	case errors.As(err, &rejected):
		d.status(dep, backend.Rollout{Phase: "failed", Reason: rejected.Reason, Message: rejected.Message,
			Desired: replicas(dep), LastError: &wire.LastError{Code: rejected.Reason, Message: rejected.Message, At: now()}})
		return
	case errors.Is(err, ErrWeightsNotCached):
		d.pending(dep, "WeightsNotResident", err.Error())
		return
	case errors.Is(err, ErrNoCapacity):
		d.pending(dep, "NoSchedulableCapacity", err.Error())
		return
	case errors.As(err, &pull):
		d.status(dep, backend.Rollout{Phase: "failed", Reason: "ImagePullFailed", Message: err.Error(),
			Desired: replicas(dep), LastError: &wire.LastError{Code: "ImagePullFailed", Message: err.Error(), At: now()}})
		return
	case err != nil:
		d.Log.Warn("apply failed; retrying", "deployment", dep.DeploymentID, "error", err)
		return
	}
	if err := d.Backend.Prune(ctx, dep, applied.Name); err != nil {
		d.Log.Warn("prune failed", "deployment", dep.DeploymentID, "error", err)
	}
	out, err := d.Backend.Observe(ctx, dep)
	if err != nil {
		d.Log.Warn("observe failed", "deployment", dep.DeploymentID, "error", err)
		return
	}
	d.status(dep, out)
}

func now() string { return time.Now().UTC().Format(time.RFC3339Nano) }

func (d *Driver) pending(dep wire.DeploymentDesired, reason, message string) {
	d.status(dep, backend.Rollout{Phase: "pending", Reason: reason, Message: message, Desired: replicas(dep)})
}

func (d *Driver) teardown(ctx context.Context, id string) {
	if err := d.Backend.Delete(ctx, id); err != nil {
		d.Log.Warn("teardown failed", "deployment", id, "error", err)
		return
	}
	d.send(wire.DeploymentObserved{DeploymentID: id, ClusterID: d.ClusterID, Phase: "terminating",
		ReplicaIDs: []string{}, ObservedAt: now()})
	d.release(id, nil)
	d.mu.Lock()
	delete(d.reported, id)
	d.mu.Unlock()
}

// status reports one rollout: the deployment, its replicas, and its allocations with the ones it
// stopped holding restated as holding nothing, so the plane frees them.
func (d *Driver) status(dep wire.DeploymentDesired, out backend.Rollout) {
	ids := []string{}
	for _, r := range out.Replicas {
		ids = append(ids, r.ReplicaID)
	}
	at := out.ObservedAt
	if at.IsZero() {
		at = time.Now().UTC()
	}
	payload := wire.DeploymentObserved{
		DeploymentID: dep.DeploymentID, ClusterID: d.ClusterID, Generation: dep.Generation,
		Phase: out.Phase, Reason: out.Reason, Message: out.Message,
		DesiredReplicas: out.Desired, ReadyReplicas: out.Ready, UpdatedReplicas: out.Updated,
		UnavailableReplicas: out.Unavailable, LastError: out.LastError, ReplicaIDs: ids,
		ObservedAt: at.Format(time.RFC3339Nano),
	}
	if out.Replicas == nil {
		payload.ReplicaIDs = nil
	}
	d.send(payload)
	if out.Replicas == nil {
		return
	}
	for _, r := range out.Replicas {
		if err := d.Reporter.Send(typeReplicaObserved, r.ReplicaID, r); err != nil {
			d.Log.Warn("could not report replica", "replica", r.Name, "error", err)
		}
	}
	d.release(dep.DeploymentID, out.Allocations)
}

func (d *Driver) release(id string, current []wire.AllocationObserved) {
	next := map[string]wire.AllocationObserved{}
	for _, a := range current {
		next[a.AllocationID] = a
		if err := d.Reporter.Send(typeAllocationObserved, a.AllocationID, a); err != nil {
			d.Log.Warn("could not report allocation", "allocation", a.AllocationID, "error", err)
		}
	}
	d.mu.Lock()
	prev := d.held[id]
	if len(next) == 0 {
		delete(d.held, id)
	} else {
		d.held[id] = next
	}
	d.mu.Unlock()
	for aid, a := range prev {
		if _, ok := next[aid]; ok {
			continue
		}
		a.DeviceIDs = []string{}
		a.ObservedAt = now()
		if err := d.Reporter.Send(typeAllocationObserved, aid, a); err != nil {
			d.Log.Warn("could not release allocation", "allocation", aid, "error", err)
		}
	}
}

// send ships deployment.observed unless it says what the last one said.
func (d *Driver) send(p wire.DeploymentObserved) {
	key := fmt.Sprintf("%d|%s|%s|%s|%d/%d/%d|%s", p.Generation, p.Phase, p.Reason, p.Message,
		p.ReadyReplicas, p.DesiredReplicas, p.UpdatedReplicas, strings.Join(p.ReplicaIDs, ","))
	d.mu.Lock()
	if d.reported[p.DeploymentID] == key {
		d.mu.Unlock()
		return
	}
	d.reported[p.DeploymentID] = key
	d.mu.Unlock()
	if err := d.Reporter.Send(typeDeploymentObserved, p.DeploymentID, p); err != nil {
		d.Log.Warn("could not report deployment", "deployment", p.DeploymentID, "error", err)
		d.mu.Lock()
		delete(d.reported, p.DeploymentID)
		d.mu.Unlock()
	}
}
