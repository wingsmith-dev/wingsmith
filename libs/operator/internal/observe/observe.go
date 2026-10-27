package observe

import (
	"context"
	"fmt"
	"log/slog"
	"reflect"
	stdsync "sync"
	"time"

	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// Wire types this loop produces.
const (
	TypeClusterObserved    = "cluster.observed"
	TypeNodeObserved       = "node.observed"
	TypeNodeEvent          = "node.event"
	TypeDeviceObserved     = "device.observed"
	TypeDeviceEvent        = "device.event"
	TypeTopologyObserved   = "topology.observed"
	TypeCapacityObserved   = "capacity.observed"
	TypeWorkloadDiscovered = "workload.discovered"
)

const (
	membershipJoined = "joined"
	defaultPool      = "default"
	unknownClass     = "unknown"
	resyncScope      = "supply"
)

// Sender is the half of sync.Client this loop needs. Narrowing it keeps the observation loop
// testable without a socket, and makes it obvious that nothing here reads the stream.
type Sender interface {
	Send(t syncpkg.MessageType, subject string, payload any) error
	SendCorrelated(t syncpkg.MessageType, subject string, payload any, replyTo string) (string, error)
}

// Observer is the upward loop. It discovers the cluster's supply on an interval, projects it into
// contract types, and sends what changed.
//
// Only changed objects go out. The stream is full-object and at-least-once, so re-sending an
// unchanged node buys nothing and spends the send queue's replacement slots on frames the hub
// already has. What guarantees the hub is not left behind is resync, not repetition.
type Observer struct {
	ClusterID       string
	OperatorVersion string

	// ReadOnly mirrors config.Config.ReadOnly onto every projection, so the plane can see that
	// this operator applies nothing. It belongs on the upward loop instead of being inferred
	// downstream: whether the reconcile loop is running is a fact only this process holds, and
	// the plane cannot tell an operator that declines to act from one that has not acted yet.
	ReadOnly   bool
	Discoverer Discoverer
	Sender     Sender
	Interval   time.Duration
	Log        *slog.Logger

	// Devices is where each tick publishes which pod holds which accelerator, for the reconcile
	// loop to read. Optional: left nil, this loop behaves exactly as it did before and the
	// reconcile loop reports no allocations instead of guessing at them.
	Devices *DeviceIndex

	// Capabilities is where each tick publishes what the cluster can do, for the reconcile loop
	// to gate on. Optional: left nil, the reconcile loop falls back to its configured flag.
	Capabilities *Capability

	// Timeout bounds a single discovery. Without it a discoverer that blocks forever takes the
	// whole loop with it silently: the process stays up, the hub session stays healthy because
	// heartbeats run on another goroutine, and the plane goes on serving the last projection as
	// though it were current. A tick that cannot finish must fail loudly instead of hanging.
	Timeout time.Duration

	// ResyncInterval is how often the loop restates its whole inventory even though nothing
	// asked it to.
	//
	// Incremental observation cannot express a removal: a node that is gone is absent from the
	// projection, and absence is only evidence against a complete snapshot. That makes resync
	// the only thing that can retire a departed node from the plane, and until this existed the
	// only trigger for one was a reconnect the hub declared fresh. An operator that stayed
	// connected therefore accumulated ghosts indefinitely. This bounds that to one interval.
	ResyncInterval time.Duration

	mu       stdsync.Mutex
	sender   Sender
	last     projection
	seen     bool
	lastTick time.Time

	// resyncMu serialises resyncs. The hub opens one epoch per cluster, so two overlapping
	// snapshots would interleave into it and the count check would reject the pair.
	resyncMu stdsync.Mutex
}

// New fills in an Observer's defaults and returns it.
func New(o *Observer) *Observer {
	if o.Interval <= 0 {
		o.Interval = 30 * time.Second
	}
	if o.Timeout <= 0 {
		o.Timeout = 2 * o.Interval
	}
	if o.ResyncInterval <= 0 {
		o.ResyncInterval = 15 * time.Minute
	}
	if o.Log == nil {
		o.Log = slog.Default()
	}
	o.Log = o.Log.With("component", "observe")
	o.sender = o.Sender
	return o
}

// Since reports how long ago a discovery last completed, and whether one ever has.
//
// Exposed so the readiness probe can fail on a loop that has stopped observing. A wedged loop is
// invisible from outside the process, it neither exits nor logs, so something has to ask.
func (o *Observer) Since(now time.Time) (time.Duration, bool) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.lastTick.IsZero() {
		return 0, false
	}
	return now.Sub(o.lastTick), true
}

// Bind supplies the Sender after construction, which the wiring needs because the sync client
// takes this loop's Resync as part of its own options: one of the two has to be built first.
func (o *Observer) Bind(s Sender) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.sender = s
}

func (o *Observer) send() (Sender, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.sender == nil {
		return nil, fmt.Errorf("observe: no sender bound")
	}
	return o.sender, nil
}

// Run observes on an interval until ctx is cancelled.
//
// A failed discovery is logged and the previous projection is kept: a cluster we could not read
// is not a cluster with nothing in it, and emitting the difference would tell the hub every node
// had gone away.
func (o *Observer) Run(ctx context.Context) error {
	ticker := time.NewTicker(o.Interval)
	defer ticker.Stop()
	resync := time.NewTicker(o.ResyncInterval)
	defer resync.Stop()
	for {
		if err := o.Tick(ctx); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			o.Log.Error("observation failed", "error", err)
		}
		select {
		case <-ctx.Done():
			return nil
		case <-resync.C:
			if err := o.Resync(ctx); err != nil {
				if ctx.Err() != nil {
					return nil
				}
				o.Log.Error("periodic resync failed", "error", err)
			}
		case <-ticker.C:
		}
	}
}

// discover runs one discovery under Timeout.
//
// The deadline is the point: every discoverer this has ever had reaches the network, and a
// read with no bound is a loop with no bound.
func (o *Observer) discover(ctx context.Context) (Snapshot, error) {
	ctx, cancel := context.WithTimeout(ctx, o.Timeout)
	defer cancel()
	return o.Discoverer.Discover(ctx)
}

// Tick performs one discovery and sends whatever it changed.
func (o *Observer) Tick(ctx context.Context) error {
	snapshot, err := o.discover(ctx)
	if err != nil {
		return fmt.Errorf("discover: %w", err)
	}
	snapshot.normalise()
	o.Devices.Publish(snapshot.Devices)
	o.Capabilities.Publish(snapshot.Multinode)
	next := o.project(snapshot)

	o.mu.Lock()
	prev, seen := o.last, o.seen
	o.last, o.seen = next, true
	o.lastTick = time.Now()
	o.mu.Unlock()

	if err := o.sendChanged(prev, next, seen); err != nil {
		return err
	}
	return o.sendEvents(prev, next, seen, snapshot.At)
}

// Resync streams the complete observed inventory as its own correlated stream, per PROTOCOL §4.
// It is called by the sync client whenever the hub grants a fresh session, and the count in
// stream.end is the number of frames actually sent.
func (o *Observer) Resync(ctx context.Context) error {
	o.resyncMu.Lock()
	defer o.resyncMu.Unlock()

	snapshot, err := o.discover(ctx)
	if err != nil {
		o.Log.Error("resync discovery failed; streaming the last known projection", "error", err)
	} else {
		snapshot.normalise()
		o.Devices.Publish(snapshot.Devices)
		o.Capabilities.Publish(snapshot.Multinode)
		next := o.project(snapshot)
		o.mu.Lock()
		o.last, o.seen = next, true
		o.lastTick = time.Now()
		o.mu.Unlock()
	}

	o.mu.Lock()
	current, seen := o.last, o.seen
	o.mu.Unlock()

	// A snapshot is only complete if it came from a discovery that worked. Saying otherwise is
	// not a white lie: `complete` is the sole permission the plane needs to delete everything
	// absent from the stream, so a failed discovery that still claimed completeness would prune
	// this cluster's entire inventory, and before the loop has ever seen anything, the
	// projection is empty and that is exactly what would happen.
	complete := err == nil && seen
	if !complete && !seen {
		return fmt.Errorf("resync: nothing observed yet: %w", err)
	}

	requestID, err := o.openStream(wire.ResyncBegin{Scope: resyncScope})
	if err != nil {
		return fmt.Errorf("open resync: %w", err)
	}

	count := 0
	send := func(t syncpkg.MessageType, subject string, payload any) error {
		if err := o.pushCorrelated(t, subject, payload, requestID); err != nil {
			return err
		}
		count++
		return nil
	}
	if err := send(TypeClusterObserved, current.cluster.ClusterID, current.cluster); err != nil {
		return err
	}
	for _, n := range current.nodes {
		if err := send(TypeNodeObserved, n.NodeID, n); err != nil {
			return err
		}
	}
	for _, d := range current.devices {
		if err := send(TypeDeviceObserved, d.DeviceID, d); err != nil {
			return err
		}
	}
	for _, t := range current.domains {
		if err := send(TypeTopologyObserved, t.DomainID, t); err != nil {
			return err
		}
	}
	for _, c := range current.capacity {
		if err := send(TypeCapacityObserved, c.Pool, c); err != nil {
			return err
		}
	}
	for _, w := range current.workloads {
		if err := send(TypeWorkloadDiscovered, w.WorkloadID, w); err != nil {
			return err
		}
	}

	if err := o.pushCorrelated(syncpkg.TypeStreamEnd, "", wire.StreamEnd{Count: count, Complete: complete}, requestID); err != nil {
		return fmt.Errorf("close resync: %w", err)
	}
	o.Log.Info("streamed observed inventory", "frames", count, "complete", complete)
	return nil
}

func (o *Observer) sendChanged(prev, next projection, seen bool) error {
	if !seen || !sameCluster(prev.cluster, next.cluster) {
		if err := o.push(TypeClusterObserved, next.cluster.ClusterID, next.cluster); err != nil {
			return err
		}
	}
	prevNodes := indexNodes(prev.nodes)
	for _, n := range next.nodes {
		if seen {
			if old, ok := prevNodes[n.NodeID]; ok && sameNode(old, n) {
				continue
			}
		}
		if err := o.push(TypeNodeObserved, n.NodeID, n); err != nil {
			return err
		}
	}
	prevDevices := indexDevices(prev.devices)
	for _, d := range next.devices {
		if seen {
			if old, ok := prevDevices[d.DeviceID]; ok && sameDevice(old, d) {
				continue
			}
		}
		if err := o.push(TypeDeviceObserved, d.DeviceID, d); err != nil {
			return err
		}
	}
	prevDomains := indexDomains(prev.domains)
	for _, t := range next.domains {
		if seen {
			if old, ok := prevDomains[t.DomainID]; ok && sameDomain(old, t) {
				continue
			}
		}
		if err := o.push(TypeTopologyObserved, t.DomainID, t); err != nil {
			return err
		}
	}
	prevCapacity := indexCapacity(prev.capacity)
	for _, c := range next.capacity {
		if seen {
			if old, ok := prevCapacity[c.Pool]; ok && sameCapacity(old, c) {
				continue
			}
		}
		if err := o.push(TypeCapacityObserved, c.Pool, c); err != nil {
			return err
		}
	}
	prevWorkloads := indexWorkloads(prev.workloads)
	for _, w := range next.workloads {
		if seen {
			if old, ok := prevWorkloads[w.WorkloadID]; ok && sameWorkload(old, w) {
				continue
			}
		}
		if err := o.push(TypeWorkloadDiscovered, w.WorkloadID, w); err != nil {
			return err
		}
	}
	return nil
}

// sendEvents reports transitions. An event is something that happened, so it is only ever derived
// from a difference between two readings, never from a state that merely holds.
func (o *Observer) sendEvents(prev, next projection, seen bool, at time.Time) error {
	occurred := formatTime(at)
	prevNodes := indexNodes(prev.nodes)
	nextNodes := indexNodes(next.nodes)

	for _, n := range next.nodes {
		old, existed := prevNodes[n.NodeID]
		switch {
		case !existed:
			// A first reading is not a transition. Without this guard every restart would
			// replay a join for the whole fleet, and an event that did not happen is worse
			// than one that was merely not reported: the observed frames carry membership.
			if !seen {
				continue
			}
			if err := o.nodeEvent(n.NodeID, "joined", "discovered", "node "+n.Name+" joined the cluster", occurred); err != nil {
				return err
			}
		default:
			if !old.Cordoned && n.Cordoned {
				if err := o.nodeEvent(n.NodeID, "cordoned", "unschedulable", "node "+n.Name+" was marked unschedulable", occurred); err != nil {
					return err
				}
			}
			if old.Cordoned && !n.Cordoned {
				if err := o.nodeEvent(n.NodeID, "uncordoned", "schedulable", "node "+n.Name+" was marked schedulable again", occurred); err != nil {
					return err
				}
			}
			if old.Ready && !n.Ready {
				if err := o.nodeEvent(n.NodeID, "degraded", "not_ready", "node "+n.Name+" stopped reporting ready", occurred); err != nil {
					return err
				}
			}
		}
	}
	if seen {
		for _, old := range prev.nodes {
			if _, ok := nextNodes[old.NodeID]; !ok {
				if err := o.nodeEvent(old.NodeID, "left", "not_found", "node "+old.Name+" is no longer a member of the cluster", occurred); err != nil {
					return err
				}
			}
		}
	}

	prevDevices := indexDevices(prev.devices)
	nextDevices := indexDevices(next.devices)
	for _, d := range next.devices {
		old, existed := prevDevices[d.DeviceID]
		if !existed {
			continue
		}
		switch {
		case old.Health == HealthHealthy && d.Health == HealthUnhealthy:
			if err := o.deviceEvent(d.DeviceID, d.NodeID, "degraded", "health_check_failed",
				"device "+d.UUID+" reports unhealthy", occurred); err != nil {
				return err
			}
		case old.Health == HealthUnhealthy && d.Health == HealthHealthy:
			if err := o.deviceEvent(d.DeviceID, d.NodeID, "recovered", "health_check_passed",
				"device "+d.UUID+" reports healthy again", occurred); err != nil {
				return err
			}
		}
	}
	if seen {
		for _, old := range prev.devices {
			if _, ok := nextDevices[old.DeviceID]; ok {
				continue
			}
			// A device on a node that has itself left did not fall off a bus. The node.event
			// already accounts for the disappearance, and two explanations of one loss is one
			// too many.
			if _, nodeRemains := nextNodes[old.NodeID]; !nodeRemains {
				continue
			}
			if err := o.deviceEvent(old.DeviceID, old.NodeID, "fell_off_bus", "gpu_lost",
				"device "+old.UUID+" is no longer present on its node", occurred); err != nil {
				return err
			}
		}
	}
	return nil
}

// push enqueues an uncorrelated frame. The subject is the id of the object described, which is
// what PROTOCOL §6 replaces on when the queue is full.
func (o *Observer) push(t syncpkg.MessageType, subject string, payload any) error {
	s, err := o.send()
	if err != nil {
		return err
	}
	return s.Send(t, subject, payload)
}

func (o *Observer) pushCorrelated(t syncpkg.MessageType, subject string, payload any, replyTo string) error {
	s, err := o.send()
	if err != nil {
		return err
	}
	_, err = s.SendCorrelated(t, subject, payload, replyTo)
	return err
}

// openStream sends resync.begin and returns the id every frame in the snapshot must correlate to.
func (o *Observer) openStream(begin wire.ResyncBegin) (string, error) {
	s, err := o.send()
	if err != nil {
		return "", err
	}
	return s.SendCorrelated(syncpkg.TypeResyncBegin, "", begin, "")
}

func (o *Observer) nodeEvent(nodeID, event, reason, message, at string) error {
	return o.push(TypeNodeEvent, nodeID, wire.NodeEvent{
		NodeID:     nodeID,
		ClusterID:  o.ClusterID,
		Event:      event,
		Reason:     reason,
		Message:    message,
		OccurredAt: at,
	})
}

func (o *Observer) deviceEvent(deviceID, nodeID, event, reason, message, at string) error {
	return o.push(TypeDeviceEvent, deviceID, wire.DeviceEvent{
		DeviceID:   deviceID,
		NodeID:     nodeID,
		ClusterID:  o.ClusterID,
		Event:      event,
		Reason:     reason,
		Message:    message,
		OccurredAt: at,
	})
}

func indexNodes(in []wire.NodeObserved) map[string]wire.NodeObserved {
	out := make(map[string]wire.NodeObserved, len(in))
	for _, n := range in {
		out[n.NodeID] = n
	}
	return out
}

func indexDevices(in []wire.DeviceObserved) map[string]wire.DeviceObserved {
	out := make(map[string]wire.DeviceObserved, len(in))
	for _, d := range in {
		out[d.DeviceID] = d
	}
	return out
}

func indexDomains(in []wire.TopologyObserved) map[string]wire.TopologyObserved {
	out := make(map[string]wire.TopologyObserved, len(in))
	for _, t := range in {
		out[t.DomainID] = t
	}
	return out
}

func indexWorkloads(in []wire.WorkloadDiscovered) map[string]wire.WorkloadDiscovered {
	out := make(map[string]wire.WorkloadDiscovered, len(in))
	for _, w := range in {
		out[w.WorkloadID] = w
	}
	return out
}

func indexCapacity(in []wire.CapacityObserved) map[string]wire.CapacityObserved {
	out := make(map[string]wire.CapacityObserved, len(in))
	for _, c := range in {
		out[c.Pool] = c
	}
	return out
}

// The observed_at stamp is excluded from every comparison below: it moves on every tick, and a
// clock reading is not an observation changing.
// sameCluster ignores the observation timestamp, like every other comparison here: the stamp moves
// on every tick, and comparing it would put a frame on the wire for a cluster nothing changed on.
func sameCluster(a, b wire.ClusterObserved) bool {
	a.ObservedAt, b.ObservedAt = "", ""
	return a == b
}

func sameNode(a, b wire.NodeObserved) bool {
	a.ObservedAt, b.ObservedAt = "", ""
	return reflect.DeepEqual(a, b)
}

func sameDevice(a, b wire.DeviceObserved) bool {
	a.ObservedAt, b.ObservedAt = "", ""
	return reflect.DeepEqual(a, b)
}

func sameDomain(a, b wire.TopologyObserved) bool {
	a.ObservedAt, b.ObservedAt = "", ""
	return reflect.DeepEqual(a, b)
}

func sameCapacity(a, b wire.CapacityObserved) bool {
	a.ObservedAt, b.ObservedAt = "", ""
	return reflect.DeepEqual(a, b)
}

func sameWorkload(a, b wire.WorkloadDiscovered) bool {
	a.ObservedAt, b.ObservedAt = "", ""
	return reflect.DeepEqual(a, b)
}
