package observe

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"reflect"
	"testing"
	"time"

	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

type frame struct {
	typ     syncpkg.MessageType
	subject string
	payload any
	replyTo string
}

// recordingSender stands in for sync.Client. It records what was enqueued, in order, which is the
// only thing the loop is responsible for: everything past the queue is the client's contract.
type recordingSender struct {
	frames []frame
	nextID int
	err    error
}

func (r *recordingSender) Send(t syncpkg.MessageType, subject string, payload any) error {
	_, err := r.SendCorrelated(t, subject, payload, "")
	return err
}

func (r *recordingSender) SendCorrelated(t syncpkg.MessageType, subject string, payload any, replyTo string) (string, error) {
	if r.err != nil {
		return "", r.err
	}
	r.nextID++
	r.frames = append(r.frames, frame{typ: t, subject: subject, payload: payload, replyTo: replyTo})
	return "id-" + string(rune('a'+r.nextID)), nil
}

func (r *recordingSender) reset() { r.frames = nil }

func (r *recordingSender) ofType(t syncpkg.MessageType) []frame {
	var out []frame
	for _, f := range r.frames {
		if f.typ == t {
			out = append(out, f)
		}
	}
	return out
}

func newTestObserver(t *testing.T, d Discoverer) *Observer {
	t.Helper()
	return New(&Observer{
		ClusterID:       testCluster,
		OperatorVersion: "0.4.2",
		Discoverer:      d,
		Sender:          &recordingSender{},
		Interval:        time.Hour,
		Log:             slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
}

func (o *Observer) recorder(t *testing.T) *recordingSender {
	t.Helper()
	r, ok := o.Sender.(*recordingSender)
	if !ok {
		t.Fatal("observer is not driving a recording sender")
	}
	return r
}

func sampleSnapshot() Snapshot {
	at := time.Date(2026, 8, 19, 14, 2, 11, 400_000_000, time.UTC)
	return Snapshot{
		At: at,
		Nodes: []Node{{
			Name:           "gpu-h100-a-07",
			Pool:           "h100-sxm-8x",
			Ready:          true,
			KernelVersion:  "6.8.0-45-generic",
			DriverVersion:  "570.86.15",
			RuntimeVersion: "containerd://1.7.27",
			Labels:         map[string]string{"region": "eu-west-1"},
			Conditions: []Condition{{
				Type: "memory_pressure", Status: "false", Reason: "sufficient_memory",
				Message: "node has sufficient memory available", Since: at,
			}},
			Devices: []Device{
				{
					UUID: "GPU-aaaa", Index: 0, Model: "NVIDIA H100 80GB HBM3",
					DeviceClass: "h100-80gb-sxm", MemoryTotalMB: 81559, Health: HealthHealthy,
					Ecc:        Ecc{ModeEnabled: true, VolatileCorrected: 12},
					Mig:        Mig{Enabled: true, Profile: "3g.40gb", InstanceID: "1"},
					DomainKind: "nvlink", DomainKey: "clique-0",
					Allocation: &Allocation{ReplicaID: "rep_x", DeploymentID: "dep_x", ProjectID: "prj_x"},
				},
				{
					UUID: "GPU-bbbb", Index: 1, Model: "NVIDIA H100 80GB HBM3",
					DeviceClass: "h100-80gb-sxm", MemoryTotalMB: 81559, Health: HealthHealthy,
					DomainKind: "nvlink", DomainKey: "clique-0",
				},
			},
		}},
	}
}

// TestFirstTickSendsTheInventory: a first reading is state, not transitions. Every observed object
// goes out and no event does: an operator restart must not invent a fleet-wide join.
func TestFirstTickSendsTheInventory(t *testing.T) {
	o := newTestObserver(t, NewFake(sampleSnapshot()))
	r := o.recorder(t)
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}

	if got := len(r.ofType(TypeNodeObserved)); got != 1 {
		t.Fatalf("sent %d node.observed frames, want 1", got)
	}
	if got := len(r.ofType(TypeDeviceObserved)); got != 2 {
		t.Fatalf("sent %d device.observed frames, want 2", got)
	}
	if got := len(r.ofType(TypeTopologyObserved)); got != 1 {
		t.Fatalf("sent %d topology.observed frames, want 1", got)
	}
	if got := len(r.ofType(TypeCapacityObserved)); got != 1 {
		t.Fatalf("sent %d capacity.observed frames, want 1", got)
	}
	if got := len(r.ofType(TypeNodeEvent)) + len(r.ofType(TypeDeviceEvent)); got != 0 {
		t.Fatalf("a first reading produced %d events; it is not a transition", got)
	}

	node := r.ofType(TypeNodeObserved)[0].payload.(wire.NodeObserved)
	assertObservedID(t, "nod_", node.NodeID)
	if node.Membership != membershipJoined || !node.Ready {
		t.Fatalf("node projected as %+v", node)
	}
	if node.OperatorVersion != "0.4.2" {
		t.Fatalf("node carries agent version %q", node.OperatorVersion)
	}
	if len(node.DeviceIDs) != 2 {
		t.Fatalf("node names %d devices, want 2", len(node.DeviceIDs))
	}
	if node.ObservedAt != "2026-08-19T14:02:11.400Z" {
		t.Fatalf("observed_at is %q", node.ObservedAt)
	}

	dev := r.ofType(TypeDeviceObserved)[0].payload.(wire.DeviceObserved)
	assertObservedID(t, "dev_", dev.DeviceID)
	if dev.NodeID != node.NodeID {
		t.Fatal("device does not name its node by the id the node minted for itself")
	}
	if dev.TopologyDomainID == nil || dev.Mig.Profile == nil || *dev.Mig.Profile != "3g.40gb" {
		t.Fatalf("device projected as %+v", dev)
	}
	if dev.Ecc.VolatileCorrected != 12 {
		t.Fatal("ECC counters are health, and must survive the projection")
	}

	dom := r.ofType(TypeTopologyObserved)[0].payload.(wire.TopologyObserved)
	if dom.Kind != "nvlink" || len(dom.DeviceIDs) != 2 {
		t.Fatalf("domain projected as %+v", dom)
	}
	if len(dom.NodeIDs) != 1 || dom.NodeIDs[0] != node.NodeID || dom.SpansNodes {
		t.Fatalf("a single-node domain projected as %+v", dom)
	}

	cap := r.ofType(TypeCapacityObserved)[0].payload.(wire.CapacityObserved)
	if len(cap.Classes) != 1 {
		t.Fatalf("capacity rolled up %d classes, want 1", len(cap.Classes))
	}
	if c := cap.Classes[0]; c.Total != 2 || c.Allocated != 1 || c.Free != 1 || c.Unhealthy != 0 {
		t.Fatalf("capacity rollup is %+v", c)
	}
}

// TestDomainSpanningNodes: a fabric shared by two machines is one domain that names both, not one
// domain attributed to whichever node happened to be walked first.
func TestDomainSpanningNodes(t *testing.T) {
	s := sampleSnapshot()
	s.Nodes = append(s.Nodes, Node{
		Name: "gpu-h100-a-08", Pool: "h100-sxm-8x", Ready: true,
		Devices: []Device{{
			UUID: "GPU-cccc", Index: 0, DeviceClass: "h100-80gb-sxm",
			MemoryTotalMB: 81559, Health: HealthHealthy,
			DomainKind: "nvlink", DomainKey: "clique-0",
		}},
	})
	o := newTestObserver(t, NewFake(s))
	r := o.recorder(t)
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}

	domains := r.ofType(TypeTopologyObserved)
	if len(domains) != 1 {
		t.Fatalf("one fabric across two nodes produced %d domains", len(domains))
	}
	dom := domains[0].payload.(wire.TopologyObserved)
	if !dom.SpansNodes {
		t.Fatalf("a domain over two nodes does not report spans_nodes: %+v", dom)
	}
	want := []string{NodeID(testCluster, "gpu-h100-a-07"), NodeID(testCluster, "gpu-h100-a-08")}
	if !reflect.DeepEqual(dom.NodeIDs, want) {
		t.Fatalf("domain names %v, want %v", dom.NodeIDs, want)
	}
	if len(dom.DeviceIDs) != 3 {
		t.Fatalf("domain names %d devices, want 3", len(dom.DeviceIDs))
	}
}

// TestSubjectIsTheObjectID is what makes PROTOCOL §6 replacement safe: two readings of one object
// share a subject, and two different objects never do.
func TestSubjectIsTheObjectID(t *testing.T) {
	o := newTestObserver(t, NewFake(sampleSnapshot()))
	r := o.recorder(t)
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}
	for _, f := range r.frames {
		if f.subject == "" {
			t.Fatalf("%s frame has no subject; the queue cannot replace it", f.typ)
		}
	}
	seen := map[string]bool{}
	for _, f := range r.ofType(TypeDeviceObserved) {
		if seen[f.subject] {
			t.Fatalf("two devices share subject %q", f.subject)
		}
		seen[f.subject] = true
		if f.subject != f.payload.(wire.DeviceObserved).DeviceID {
			t.Fatal("device subject is not the device id")
		}
	}
	if s := r.ofType(TypeNodeObserved)[0].subject; s != r.ofType(TypeNodeObserved)[0].payload.(wire.NodeObserved).NodeID {
		t.Fatalf("node subject %q is not the node id", s)
	}
	if s := r.ofType(TypeCapacityObserved)[0].subject; s != "h100-sxm-8x" {
		t.Fatalf("capacity subject %q is not the pool", s)
	}
}

// TestUnchangedClusterStaysOffTheWire: the stream is at-least-once and full-object, so repeating
// an unchanged reading spends replacement slots on frames the hub already holds.
func TestUnchangedClusterStaysOffTheWire(t *testing.T) {
	fake := NewFake(sampleSnapshot())
	o := newTestObserver(t, fake)
	r := o.recorder(t)
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}
	r.reset()

	later := sampleSnapshot()
	later.At = later.At.Add(time.Minute)
	fake.Set(later)
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}
	if len(r.frames) != 0 {
		t.Fatalf("an unchanged cluster produced %d frames: %+v", len(r.frames), r.frames)
	}
}

func TestTransitionsBecomeEvents(t *testing.T) {
	fake := NewFake(sampleSnapshot())
	o := newTestObserver(t, fake)
	r := o.recorder(t)
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}

	t.Run("device degrades and recovers", func(t *testing.T) {
		r.reset()
		s := sampleSnapshot()
		s.Nodes[0].Devices[1].Health = HealthUnhealthy
		fake.Set(s)
		if err := o.Tick(t.Context()); err != nil {
			t.Fatal(err)
		}
		events := r.ofType(TypeDeviceEvent)
		if len(events) != 1 {
			t.Fatalf("sent %d device events, want 1", len(events))
		}
		ev := events[0].payload.(wire.DeviceEvent)
		if ev.Event != "degraded" || ev.DeviceID != DeviceID(testCluster, "GPU-bbbb") {
			t.Fatalf("event is %+v", ev)
		}
		if len(r.ofType(TypeDeviceObserved)) != 1 {
			t.Fatal("the degraded device's observed state did not follow its event")
		}

		r.reset()
		fake.Set(sampleSnapshot())
		if err := o.Tick(t.Context()); err != nil {
			t.Fatal(err)
		}
		if ev := r.ofType(TypeDeviceEvent); len(ev) != 1 || ev[0].payload.(wire.DeviceEvent).Event != "recovered" {
			t.Fatalf("recovery produced %+v", ev)
		}
	})

	t.Run("node is cordoned then goes not ready", func(t *testing.T) {
		r.reset()
		s := sampleSnapshot()
		s.Nodes[0].Cordoned = true
		s.Nodes[0].Ready = false
		fake.Set(s)
		if err := o.Tick(t.Context()); err != nil {
			t.Fatal(err)
		}
		got := map[string]bool{}
		for _, f := range r.ofType(TypeNodeEvent) {
			got[f.payload.(wire.NodeEvent).Event] = true
		}
		if !got["cordoned"] || !got["degraded"] {
			t.Fatalf("cordon and readiness loss produced %v", got)
		}
	})

	t.Run("node is uncordoned", func(t *testing.T) {
		r.reset()
		fake.Set(sampleSnapshot())
		if err := o.Tick(t.Context()); err != nil {
			t.Fatal(err)
		}
		events := r.ofType(TypeNodeEvent)
		if len(events) != 1 || events[0].payload.(wire.NodeEvent).Event != "uncordoned" {
			t.Fatalf("returning a node to service produced %+v", events)
		}
	})

	t.Run("a node joins", func(t *testing.T) {
		r.reset()
		s := sampleSnapshot()
		s.Nodes = append(s.Nodes, Node{Name: "gpu-h100-a-08", Pool: "h100-sxm-8x", Ready: true})
		fake.Set(s)
		if err := o.Tick(t.Context()); err != nil {
			t.Fatal(err)
		}
		events := r.ofType(TypeNodeEvent)
		if len(events) != 1 || events[0].payload.(wire.NodeEvent).Event != "joined" {
			t.Fatalf("a new node produced %+v", events)
		}
	})

	t.Run("a device falls off the bus", func(t *testing.T) {
		r.reset()
		s := sampleSnapshot()
		s.Nodes = append(s.Nodes, Node{Name: "gpu-h100-a-08", Pool: "h100-sxm-8x", Ready: true})
		s.Nodes[0].Devices = s.Nodes[0].Devices[:1]
		fake.Set(s)
		if err := o.Tick(t.Context()); err != nil {
			t.Fatal(err)
		}
		events := r.ofType(TypeDeviceEvent)
		if len(events) != 1 {
			t.Fatalf("sent %d device events, want 1", len(events))
		}
		if ev := events[0].payload.(wire.DeviceEvent); ev.Event != "fell_off_bus" {
			t.Fatalf("event is %+v", ev)
		}
	})

	t.Run("a node leaves", func(t *testing.T) {
		r.reset()
		s := sampleSnapshot()
		s.Nodes[0].Devices = s.Nodes[0].Devices[:1]
		fake.Set(s)
		if err := o.Tick(t.Context()); err != nil {
			t.Fatal(err)
		}
		events := r.ofType(TypeNodeEvent)
		if len(events) != 1 || events[0].payload.(wire.NodeEvent).Event != "left" {
			t.Fatalf("a departed node produced %+v", events)
		}
		// The devices of a node that left are not reported as having fallen off a bus: the
		// machine is gone, and two explanations of one disappearance is one too many.
		if got := len(r.ofType(TypeDeviceEvent)); got != 0 {
			t.Fatalf("a departing node also produced %d device events", got)
		}
	})
}

// TestFailedDiscoveryKeepsTheLastProjection: a cluster we could not read is not an empty cluster,
// and emitting the difference would tell the hub the fleet had gone away.
func TestFailedDiscoveryKeepsTheLastProjection(t *testing.T) {
	fake := NewFake(sampleSnapshot())
	o := newTestObserver(t, fake)
	r := o.recorder(t)
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}
	r.reset()

	fake.SetError(errors.New("apiserver unreachable"))
	if err := o.Tick(t.Context()); err == nil {
		t.Fatal("a failed discovery was reported as a successful tick")
	}
	if len(r.frames) != 0 {
		t.Fatalf("a failed discovery produced %d frames", len(r.frames))
	}

	fake.Set(sampleSnapshot())
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}
	if len(r.frames) != 0 {
		t.Fatalf("the recovered tick re-sent %d unchanged frames", len(r.frames))
	}
}

// TestResyncStreamsTheWholeInventory is PROTOCOL §4: the operator opens its own stream, sends
// everything it can see under one reply_to, and closes with a count of what it actually sent.
func TestResyncStreamsTheWholeInventory(t *testing.T) {
	o := newTestObserver(t, NewFake(sampleSnapshot()))
	r := o.recorder(t)
	if err := o.Resync(t.Context()); err != nil {
		t.Fatal(err)
	}

	if len(r.frames) < 3 {
		t.Fatalf("resync sent %d frames", len(r.frames))
	}
	first, last := r.frames[0], r.frames[len(r.frames)-1]
	if first.typ != syncpkg.TypeResyncBegin {
		t.Fatalf("resync opened with %s", first.typ)
	}
	if scope := first.payload.(wire.ResyncBegin).Scope; scope != "supply" {
		t.Fatalf("resync scope is %q", scope)
	}
	if last.typ != syncpkg.TypeStreamEnd {
		t.Fatalf("resync closed with %s", last.typ)
	}

	begin := "id-b"
	for _, f := range r.frames[1:] {
		if f.replyTo != begin {
			t.Fatalf("%s frame correlates to %q, not to the resync.begin %q", f.typ, f.replyTo, begin)
		}
	}
	end := last.payload.(wire.StreamEnd)
	if !end.Complete {
		t.Fatal("a snapshot that is whole must say so, or the hub will not prune against it")
	}
	body := len(r.frames) - 2
	if end.Count != body {
		t.Fatalf("stream.end counted %d, but %d frames were sent", end.Count, body)
	}
	if body != 6 {
		t.Fatalf("resync streamed %d objects, want 6 (1 cluster, 1 node, 2 devices, 1 domain, 1 pool)", body)
	}
}

// TestResyncSurvivesAFailedDiscovery: the resync is the hub's only repair, so it streams the last
// known projection instead of declining to answer, but it does not call that snapshot complete.
// Completeness is the plane's sole permission to delete everything absent from the stream, and a
// projection nothing just confirmed is not evidence that anything is absent.
func TestResyncSurvivesAFailedDiscovery(t *testing.T) {
	fake := NewFake(sampleSnapshot())
	o := newTestObserver(t, fake)
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}
	r := o.recorder(t)
	r.reset()

	fake.SetError(errors.New("apiserver unreachable"))
	if err := o.Resync(t.Context()); err != nil {
		t.Fatal(err)
	}
	end := r.frames[len(r.frames)-1].payload.(wire.StreamEnd)
	if end.Count != 6 {
		t.Fatalf("degraded resync streamed %d frames, want the 6 it last knew about", end.Count)
	}
	if end.Complete {
		t.Fatal("a snapshot built on a failed discovery must not claim to be whole: the hub would prune against it")
	}
}

// TestResyncRefusesBeforeAnythingIsObserved: the empty projection is the dangerous one. An operator
// whose very first discovery fails knows of no nodes at all, and a complete snapshot of nothing is
// a valid instruction to delete the cluster's entire inventory.
func TestResyncRefusesBeforeAnythingIsObserved(t *testing.T) {
	fake := NewFake(sampleSnapshot())
	fake.SetError(errors.New("apiserver unreachable"))
	o := newTestObserver(t, fake)

	if err := o.Resync(t.Context()); err == nil {
		t.Fatal("a resync with nothing ever observed must fail, not stream an empty snapshot")
	}
	for _, f := range o.recorder(t).frames {
		if end, ok := f.payload.(wire.StreamEnd); ok && end.Complete {
			t.Fatalf("streamed a complete snapshot of nothing: %+v", end)
		}
	}
}

// TestWithObserverJoinsBothHalves: a fresh session makes the store open its epoch for what comes
// down and the observer stream everything that goes up.
func TestWithObserverJoinsBothHalves(t *testing.T) {
	o := newTestObserver(t, NewFake(sampleSnapshot()))
	inner := &countingHandler{}
	h := WithObserver(inner, o)
	if err := h.Resync(t.Context()); err != nil {
		t.Fatal(err)
	}
	if inner.resyncs != 1 {
		t.Fatalf("inner handler saw %d resyncs", inner.resyncs)
	}
	if len(o.recorder(t).ofType(syncpkg.TypeResyncBegin)) != 1 {
		t.Fatal("the observer did not open its own stream")
	}

	inner.err = errors.New("checkpoint write failed")
	if err := h.Resync(t.Context()); err == nil {
		t.Fatal("a failed downward resync was reported as success")
	}
}

// TestSendFailureStopsTheTick: an unbound or closed sender is not a reason to advance as if the
// hub had been told.
func TestSendFailureStopsTheTick(t *testing.T) {
	o := newTestObserver(t, NewFake(sampleSnapshot()))
	o.recorder(t).err = syncpkg.ErrQueueClosed
	if err := o.Tick(t.Context()); !errors.Is(err, syncpkg.ErrQueueClosed) {
		t.Fatalf("tick returned %v", err)
	}

	unbound := New(&Observer{ClusterID: testCluster, Discoverer: NewFake(sampleSnapshot()),
		Log: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if err := unbound.Tick(t.Context()); err == nil {
		t.Fatal("an unbound observer sent into the void")
	}
	unbound.Bind(&recordingSender{})
	if err := unbound.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}
}

type countingHandler struct {
	resyncs int
	err     error
}

func (c *countingHandler) Handle(context.Context, *syncpkg.Envelope) error { return nil }

func (c *countingHandler) Resync(context.Context) error {
	c.resyncs++
	return c.err
}
