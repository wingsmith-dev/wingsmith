package sync

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	stdsync "sync"
	"testing"
	"time"
)

type recorder struct {
	mu       stdsync.Mutex
	seen     []uint64
	resyncs  int
	rejectAt uint64
}

func (r *recorder) Handle(_ context.Context, e *Envelope) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.rejectAt != 0 && e.Seq == r.rejectAt {
		return errHandlerReject
	}
	r.seen = append(r.seen, e.Seq)
	return nil
}

func (r *recorder) Resync(context.Context) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.resyncs++
	return nil
}

func (r *recorder) seenSeqs() []uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]uint64(nil), r.seen...)
}

func (r *recorder) resyncCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.resyncs
}

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func startClient(t *testing.T, h *fakeHub, mutate func(*Options)) (*Client, *recorder) {
	t.Helper()
	rec := &recorder{}
	opts := Options{
		HubURL:            h.url(),
		ClusterID:         "cluster-test",
		Token:             "token",
		HeartbeatInterval: 20 * time.Millisecond,
		LivenessTimeout:   200 * time.Millisecond,
		BackoffMin:        5 * time.Millisecond,
		BackoffMax:        20 * time.Millisecond,
		SendQueueSize:     16,
		SendQueuePolicy:   policyDropOldest,
		Handler:           rec,
		Logger:            testLogger(),
	}
	if mutate != nil {
		mutate(&opts)
	}
	c := NewClient(opts)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = c.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("client did not stop")
		}
	})
	return c, rec
}

func TestHandshakeAndDownwardDelivery(t *testing.T) {
	h := newFakeHub(t)
	_, rec := startClient(t, h, nil)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push(map[string]string{"name": "a"})
	h.push(map[string]string{"name": "b"})

	eventually(t, 2*time.Second, "two envelopes delivered", func() bool { return len(rec.seenSeqs()) == 2 })
	if got := rec.seenSeqs(); got[0] != 1 || got[1] != 2 {
		t.Fatalf("expected seqs 1,2 got %v", got)
	}
}

func TestReconnectAfterPartition(t *testing.T) {
	h := newFakeHub(t)
	c, _ := startClient(t, h, nil)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.kill()
	eventually(t, 3*time.Second, "reconnection", func() bool { return h.connectionCount() >= 2 })
	eventually(t, 3*time.Second, "session re-established", func() bool { return c.Connected() })
}

func TestResumeSkipsAlreadyDeliveredEnvelopes(t *testing.T) {
	h := newFakeHub(t)
	_, rec := startClient(t, h, nil)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push(map[string]string{"name": "a"})
	h.push(map[string]string{"name": "b"})
	eventually(t, 2*time.Second, "first two delivered", func() bool { return len(rec.seenSeqs()) == 2 })

	h.kill()
	h.push(map[string]string{"name": "c"})

	eventually(t, 3*time.Second, "third envelope delivered after resume", func() bool {
		return len(rec.seenSeqs()) == 3
	})
	got := rec.seenSeqs()
	want := []uint64{1, 2, 3}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("expected %v, got %v (resume must not redeliver)", want, got)
		}
	}
	if hello := h.lastHello(); hello.LastReceivedSeq != 2 {
		t.Fatalf("expected resume from seq 2, hello said %d", hello.LastReceivedSeq)
	}
	if rec.resyncCount() != 0 {
		t.Fatalf("expected no resync on a successful resume, got %d", rec.resyncCount())
	}
}

func TestResumeRefusedTriggersResync(t *testing.T) {
	h := newFakeHub(t)
	_, rec := startClient(t, h, nil)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push(map[string]string{"name": "a"})
	eventually(t, 2*time.Second, "first delivered", func() bool { return len(rec.seenSeqs()) == 1 })

	h.setResume(false)
	h.kill()

	eventually(t, 3*time.Second, "resync after refused resume", func() bool { return rec.resyncCount() >= 1 })
	eventually(t, 3*time.Second, "full replay after refused resume", func() bool {
		return len(rec.seenSeqs()) >= 2
	})
	if got := rec.seenSeqs(); got[1] != 1 {
		t.Fatalf("expected the stream to restart at seq 1, got %v", got)
	}
}

func TestUnacknowledgedUpwardEnvelopesAreReplayed(t *testing.T) {
	h := newFakeHub(t)
	c, _ := startClient(t, h, nil)
	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })

	if err := c.Send(TypeObserved, map[string]string{"n": "1"}); err != nil {
		t.Fatal(err)
	}
	eventually(t, 2*time.Second, "first observed received", func() bool { return len(h.upwardSeqs()) == 1 })

	h.kill()
	if err := c.Send(TypeObserved, map[string]string{"n": "2"}); err != nil {
		t.Fatal(err)
	}
	eventually(t, 3*time.Second, "second observed received after resume", func() bool {
		return len(h.upwardSeqs()) == 2
	})
	got := h.upwardSeqs()
	if got[0] != 1 || got[1] != 2 {
		t.Fatalf("expected contiguous upward seqs 1,2 got %v", got)
	}
}

func TestRejectedEnvelopeIsNotAcknowledged(t *testing.T) {
	h := newFakeHub(t)
	rec := &recorder{rejectAt: 1}
	c := NewClient(Options{
		HubURL:            h.url(),
		ClusterID:         "cluster-test",
		HeartbeatInterval: 20 * time.Millisecond,
		LivenessTimeout:   200 * time.Millisecond,
		BackoffMin:        5 * time.Millisecond,
		BackoffMax:        20 * time.Millisecond,
		SendQueueSize:     16,
		Handler:           rec,
		Logger:            testLogger(),
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = c.Run(ctx) }()

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push(map[string]string{"name": "a"})
	eventually(t, 2*time.Second, "envelope offered", func() bool { return h.lastHello().ClusterID != "" })
	time.Sleep(100 * time.Millisecond)

	h.kill()
	eventually(t, 3*time.Second, "reconnect", func() bool { return h.connectionCount() >= 2 })
	eventually(t, 2*time.Second, "resume from zero", func() bool { return h.lastHello().LastReceivedSeq == 0 })
}

func TestLivenessTimeoutForcesReconnect(t *testing.T) {
	h := newFakeHub(t)
	h.setStallHello(true)
	_, _ = startClient(t, h, func(o *Options) {
		o.HeartbeatInterval = 20 * time.Millisecond
		o.LivenessTimeout = 60 * time.Millisecond
	})

	eventually(t, 3*time.Second, "redial after a silent hub", func() bool { return h.connectionCount() >= 2 })
}

func TestBackpressureDropOldest(t *testing.T) {
	q := newSendQueue(2, policyDropOldest)
	for i := 1; i <= 4; i++ {
		if err := q.Push(&Envelope{Type: TypeObserved, Seq: uint64(i)}); err != nil {
			t.Fatal(err)
		}
	}
	if q.Len() != 2 {
		t.Fatalf("expected depth 2, got %d", q.Len())
	}
	if q.Dropped() != 2 {
		t.Fatalf("expected 2 drops, got %d", q.Dropped())
	}
	e, err := q.Pop(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if e.Seq != 3 {
		t.Fatalf("drop-oldest must keep the freshest envelopes, head was %d", e.Seq)
	}
}

func TestBackpressureDropNewest(t *testing.T) {
	q := newSendQueue(2, policyDropNewest)
	for i := 1; i <= 4; i++ {
		if err := q.Push(&Envelope{Type: TypeObserved, Seq: uint64(i)}); err != nil {
			t.Fatal(err)
		}
	}
	e, err := q.Pop(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if e.Seq != 1 {
		t.Fatalf("drop-newest must preserve the backlog, head was %d", e.Seq)
	}
	if q.Dropped() != 2 {
		t.Fatalf("expected 2 drops, got %d", q.Dropped())
	}
}

func TestBackpressureDisconnect(t *testing.T) {
	q := newSendQueue(1, policyDisconnect)
	if err := q.Push(&Envelope{Type: TypeObserved, Seq: 1}); err != nil {
		t.Fatal(err)
	}
	err := q.Push(&Envelope{Type: TypeObserved, Seq: 2})
	if err != ErrQueueFull {
		t.Fatalf("expected ErrQueueFull, got %v", err)
	}
}

func TestBackpressureDisconnectPolicySurfacesToTheCaller(t *testing.T) {
	h := newFakeHub(t)
	h.setStallHello(true)
	c, _ := startClient(t, h, func(o *Options) {
		o.SendQueueSize = 2
		o.SendQueuePolicy = policyDisconnect
		o.HeartbeatInterval = time.Second
		o.LivenessTimeout = 10 * time.Second
	})

	if err := c.Send(TypeObserved, map[string]int{"i": 1}); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(TypeObserved, map[string]int{"i": 2}); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(TypeObserved, map[string]int{"i": 3}); err != ErrQueueFull {
		t.Fatalf("expected ErrQueueFull once the bound is reached, got %v", err)
	}
	if c.QueueDepth() != 2 {
		t.Fatalf("queue exceeded its bound: %d", c.QueueDepth())
	}
}

func TestSendQueueDoesNotBlockWhileDisconnected(t *testing.T) {
	h := newFakeHub(t)
	h.setStallHello(true)
	c, _ := startClient(t, h, func(o *Options) {
		o.SendQueueSize = 4
		o.SendQueuePolicy = policyDropOldest
	})

	for i := 0; i < 100; i++ {
		if err := c.Send(TypeObserved, map[string]int{"i": i}); err != nil {
			t.Fatalf("Send blocked or failed at %d: %v", i, err)
		}
	}
	if c.QueueDepth() > 4 {
		t.Fatalf("queue exceeded its bound: %d", c.QueueDepth())
	}
}

func TestEnvelopeSequencing(t *testing.T) {
	for _, tc := range []struct {
		typ  MessageType
		want bool
	}{
		{TypeDesired, true}, {TypeObserved, true}, {TypeError, true},
		{TypePing, false}, {TypePong, false}, {TypeAck, false},
		{TypeHello, false}, {TypeHelloAck, false},
	} {
		if got := (&Envelope{Type: tc.typ}).Sequenced(); got != tc.want {
			t.Errorf("%s: Sequenced() = %v, want %v", tc.typ, got, tc.want)
		}
	}
}

func TestEnvelopeRoundTrip(t *testing.T) {
	in := &Envelope{
		Type: TypeDesired, ID: "abc", CorrelationID: "def", Seq: 7, Ack: 3,
		Timestamp: time.Now().UTC().Truncate(time.Millisecond),
		Payload:   json.RawMessage(`{"k":"v"}`),
	}
	b, err := json.Marshal(in)
	if err != nil {
		t.Fatal(err)
	}
	var out Envelope
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	if out.Seq != in.Seq || out.Ack != in.Ack || out.CorrelationID != in.CorrelationID || !out.Timestamp.Equal(in.Timestamp) {
		t.Fatalf("round trip mismatch: %+v vs %+v", out, in)
	}
}
