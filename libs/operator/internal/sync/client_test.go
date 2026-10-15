package sync

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	stdsync "sync"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

type recorder struct {
	mu        stdsync.Mutex
	seen      []uint64
	types     []string
	resyncs   int
	rejectAt  uint64
	panicAt   uint64
	panicSync bool
	// failures rejects a sequence number that many times and then applies it.
	failures map[uint64]int
}

func (r *recorder) Handle(_ context.Context, e *Envelope) error {
	r.mu.Lock()
	var seq uint64
	if e.Seq != nil {
		seq = *e.Seq
	}
	reject := r.rejectAt != 0 && seq == r.rejectAt
	if left := r.failures[seq]; left > 0 {
		r.failures[seq] = left - 1
		reject = true
	}
	doPanic := r.panicAt != 0 && seq == r.panicAt
	r.mu.Unlock()
	if doPanic {
		panic("handler exploded on frame " + e.Type)
	}
	if reject {
		return errHandlerReject
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.seen = append(r.seen, seq)
	r.types = append(r.types, e.Type)
	return nil
}

func (r *recorder) Resync(context.Context) error {
	r.mu.Lock()
	shouldPanic := r.panicSync
	r.mu.Unlock()
	if shouldPanic {
		panic("resync exploded")
	}
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

// resyncOrdering records the interleaving of resyncs and frames as a string: "r" for a resync,
// "f" for a frame. The order is the assertion — a snapshot frame applied before the resync that
// announces it would be applied into a local view still claiming to be authoritative.
type resyncOrdering struct {
	recorder
	log []byte
}

func (r *resyncOrdering) Handle(ctx context.Context, e *Envelope) error {
	if err := r.recorder.Handle(ctx, e); err != nil {
		return err
	}
	r.mu.Lock()
	r.log = append(r.log, 'f')
	r.mu.Unlock()
	return nil
}

func (r *resyncOrdering) Resync(ctx context.Context) error {
	if err := r.recorder.Resync(ctx); err != nil {
		return err
	}
	r.mu.Lock()
	r.log = append(r.log, 'r')
	r.mu.Unlock()
	return nil
}

func (r *resyncOrdering) ordering() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return string(r.log)
}

// flakyCheckpointer fails every Save once armed, which is how a partitioned API server looks to
// the client.
type flakyCheckpointer struct {
	mu     stdsync.Mutex
	cursor Cursor
	fail   bool
	saves  int
}

func (f *flakyCheckpointer) Load(context.Context) (Cursor, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.cursor, nil
}

func (f *flakyCheckpointer) Save(_ context.Context, c Cursor) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.saves++
	if f.fail {
		return errors.New("apiserver unreachable")
	}
	f.cursor = c
	return nil
}

func (f *flakyCheckpointer) arm() {
	f.mu.Lock()
	f.fail = true
	f.mu.Unlock()
}

func (f *flakyCheckpointer) current() Cursor {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.cursor
}

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func testOptions(h *fakeHub) Options {
	return Options{
		HubURL:            h.url(),
		ClusterID:         "clu_test",
		Token:             "token",
		OperatorVersion:   "0.0.1-test",
		HeartbeatInterval: 20 * time.Millisecond,
		LivenessTimeout:   200 * time.Millisecond,
		BackoffMin:        5 * time.Millisecond,
		BackoffMax:        20 * time.Millisecond,
		SendQueueSize:     16,
		Logger:            testLogger(),
	}
}

func startWith(t *testing.T, opts Options) *Client {
	t.Helper()
	return startTuned(t, opts, nil)
}

// startTuned runs a client after letting the test adjust the per-client knobs Options does not
// carry.
func startTuned(t *testing.T, opts Options, tune func(*Client)) *Client {
	t.Helper()
	c := NewClient(opts)
	if tune != nil {
		tune(c)
	}
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
	return c
}

func startClient(t *testing.T, h *fakeHub, mutate func(*Options)) (*Client, *recorder) {
	t.Helper()
	rec := &recorder{}
	opts := testOptions(h)
	opts.Handler = rec
	if mutate != nil {
		mutate(&opts)
	}
	return startWith(t, opts), rec
}

func TestHandshakeIsContractShaped(t *testing.T) {
	h := newFakeHub(t)
	startClient(t, h, nil)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	if got := h.lastAuthHeader(); got != "Bearer token" {
		t.Fatalf("token must travel as an Authorization header, got %q", got)
	}
	hello := h.lastHello()
	if hello.ClusterID != "clu_test" || hello.OperatorVersion != "0.0.1-test" {
		t.Fatalf("unexpected hello: %+v", hello)
	}
	if hello.SessionID != nil || hello.ResumeFrom != nil {
		t.Fatalf("a first connection resumes nothing: %+v", hello)
	}
	if hello.Lossy {
		t.Fatal("a first connection is not lossy")
	}
}

func TestHandshakeAndDownwardDelivery(t *testing.T) {
	h := newFakeHub(t)
	_, rec := startClient(t, h, nil)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("dep_a")
	h.push("dep_b")

	eventually(t, 2*time.Second, "two frames delivered", func() bool { return len(rec.seenSeqs()) == 2 })
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

// TestResumeFromTheDurableCursor restarts the client against a cursor that outlived it, which is
// the property PROTOCOL §4 requires: a process that has never spoken to this hub still knows
// where the stream got to.
func TestResumeFromTheDurableCursor(t *testing.T) {
	h := newFakeHub(t)
	cursor := &MemoryCheckpointer{}

	first := &recorder{}
	opts := testOptions(h)
	opts.Handler, opts.Checkpointer = first, cursor
	c := NewClient(opts)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); _ = c.Run(ctx) }()

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("dep_a")
	h.push("dep_b")
	eventually(t, 2*time.Second, "two frames delivered", func() bool { return len(first.seenSeqs()) == 2 })

	cancel()
	<-done

	saved, err := cursor.Load(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if saved.Seq != 2 {
		t.Fatalf("durable cursor = %d, want 2", saved.Seq)
	}

	second := &recorder{}
	opts = testOptions(h)
	opts.Handler, opts.Checkpointer = second, cursor
	startWith(t, opts)

	eventually(t, 3*time.Second, "restarted client reconnects", func() bool { return h.connectionCount() >= 2 })
	eventually(t, 2*time.Second, "hello carries the durable cursor", func() bool {
		hello := h.lastHello()
		return hello.ResumeFrom != nil && *hello.ResumeFrom == 2
	})
	h.push("dep_c")
	eventually(t, 2*time.Second, "stream continues", func() bool { return len(second.seenSeqs()) == 1 })
	if got := second.seenSeqs(); got[0] != 3 {
		t.Fatalf("resume redelivered already-applied frames: %v", got)
	}
}

func TestDuplicateFrameIsDropped(t *testing.T) {
	h := newFakeHub(t)
	_, rec := startClient(t, h, nil)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("dep_a")
	h.push("dep_b")
	eventually(t, 2*time.Second, "two frames delivered", func() bool { return len(rec.seenSeqs()) == 2 })

	h.pushAt(2, "dep_b")
	h.push("dep_c")
	eventually(t, 2*time.Second, "third frame delivered", func() bool { return len(rec.seenSeqs()) == 3 })
	if got := rec.seenSeqs(); got[2] != 3 {
		t.Fatalf("duplicate was applied: %v", got)
	}
	if h.connectionCount() != 1 {
		t.Fatal("a duplicate must be dropped, not treated as a protocol failure")
	}
}

func TestGapClosesTheConnection(t *testing.T) {
	h := newFakeHub(t)
	_, rec := startClient(t, h, nil)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("dep_a")
	eventually(t, 2*time.Second, "first frame delivered", func() bool { return len(rec.seenSeqs()) == 1 })

	h.pushAt(7, "dep_far")
	eventually(t, 3*time.Second, "connection closed and redialled", func() bool { return h.connectionCount() >= 2 })
	for _, seq := range rec.seenSeqs() {
		if seq == 7 {
			t.Fatal("a gap must close the connection, never be accepted as a hole")
		}
	}
}

func TestResumeRefusedCallsResyncBeforeTheSnapshot(t *testing.T) {
	h := newFakeHub(t)
	rec := &resyncOrdering{}
	opts := testOptions(h)
	opts.Handler = rec
	startWith(t, opts)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("dep_a")
	eventually(t, 2*time.Second, "first delivered", func() bool { return len(rec.seenSeqs()) == 1 })

	h.setResume(false)
	h.kill()

	eventually(t, 3*time.Second, "resync after refused resume", func() bool { return rec.resyncCount() >= 2 })
	eventually(t, 3*time.Second, "snapshot replayed", func() bool { return len(rec.seenSeqs()) >= 2 })
	if got := rec.ordering(); !strings.HasPrefix(got, "rfrf") {
		t.Fatalf("resync must precede the snapshot it announces, ordering was %q", got)
	}
	if got := rec.seenSeqs(); got[1] != 1 {
		t.Fatalf("a fresh session numbers from 1, got %v", got)
	}
}

func TestAckAdvancesOnlyOnContiguousDurableApply(t *testing.T) {
	h := newFakeHub(t)
	rec := &recorder{rejectAt: 1}
	opts := testOptions(h)
	opts.Handler = rec
	c := startWith(t, opts)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("dep_a")
	if err := c.Send("node.observed", "nod_1", wire.NodeObserved{NodeID: "nod_1"}); err != nil {
		t.Fatal(err)
	}
	eventually(t, 2*time.Second, "upward frame received", func() bool { return len(h.upwardSeqs()) == 1 })
	time.Sleep(100 * time.Millisecond)

	if got := h.highestAck(); got != 0 {
		t.Fatalf("a rejected frame must not be acknowledged, hub saw ack %d", got)
	}

	h.kill()
	eventually(t, 3*time.Second, "reconnect", func() bool { return h.connectionCount() >= 2 })
	eventually(t, 2*time.Second, "resume from the last applied frame", func() bool {
		hello := h.lastHello()
		return hello.ResumeFrom == nil || *hello.ResumeFrom == 0
	})
}

func TestARejectedFrameIsRetriedWithinTheSession(t *testing.T) {
	h := newFakeHub(t)
	rec := &recorder{failures: map[uint64]int{1: 2}}
	opts := testOptions(h)
	opts.Handler = rec
	c := startTuned(t, opts, func(c *Client) { c.applyRetryMin = time.Millisecond })

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("dep_a")
	h.push("dep_b")

	eventually(t, 2*time.Second, "both frames applied", func() bool { return len(rec.seenSeqs()) == 2 })
	// The hub reads the operator's ack off its sequenced frames, so one has to go up.
	if err := c.Send("node.observed", "nod_1", wire.NodeObserved{NodeID: "nod_1"}); err != nil {
		t.Fatal(err)
	}
	eventually(t, 2*time.Second, "ack past the retried frame", func() bool { return h.highestAck() == 2 })
	if got := rec.seenSeqs(); got[0] != 1 || got[1] != 2 {
		t.Fatalf("frames must apply in order once the retry succeeds, got %v", got)
	}
	if got := h.connectionCount(); got != 1 {
		t.Fatalf("a frame that succeeds on retry must not cost the session, saw %d connections", got)
	}
}

func TestAFrameRejectedOnEveryAttemptEndsTheSession(t *testing.T) {
	h := newFakeHub(t)
	rec := &recorder{rejectAt: 1}
	opts := testOptions(h)
	opts.Handler = rec
	startTuned(t, opts, func(c *Client) {
		c.applyAttempts = 2
		c.applyRetryMin = time.Millisecond
	})

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("dep_a")
	eventually(t, 3*time.Second, "the session ends and redials", func() bool { return h.connectionCount() >= 2 })
	hello := h.lastHello()
	if hello.ResumeFrom != nil && *hello.ResumeFrom != 0 {
		t.Fatalf("the rejected frame must be replayed, resume_from was %d", *hello.ResumeFrom)
	}
	if got := h.highestAck(); got != 0 {
		t.Fatalf("a rejected frame must not be acknowledged, hub saw ack %d", got)
	}
}

// TestPanickingHandlerIsSurvivedAndTheFrameAdvances is the PROTOCOL §6b case for a panic: a
// handler panic is treated as a permanently unappliable frame (accepted, discarded, logged), not
// as a transient failure to withhold the ack for, because a deterministic panic would otherwise
// replay forever and wedge the stream. The client must stay up and keep making progress past it.
func TestPanickingHandlerIsSurvivedAndTheFrameAdvances(t *testing.T) {
	h := newFakeHub(t)
	rec := &recorder{panicAt: 2}
	opts := testOptions(h)
	opts.Handler = rec
	c := startWith(t, opts)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("dep_a")
	h.push("dep_b")
	h.push("dep_c")

	eventually(t, 2*time.Second, "frames around the panic are applied", func() bool {
		return len(rec.seenSeqs()) == 2
	})
	if got := rec.seenSeqs(); got[0] != 1 || got[1] != 3 {
		t.Fatalf("expected seqs 1,3 (2 panicked and was discarded), got %v", got)
	}
	if h.connectionCount() != 1 {
		t.Fatal("a panicking handler must not tear down the connection")
	}

	// Ack is only visible on the hub via a carrier frame (mirroring
	// TestAckAdvancesOnlyOnContiguousDurableApply): a bare TypeAck is unsequenced and the fake
	// hub only records sequenced upward frames.
	if err := c.Send("node.observed", "nod_1", wire.NodeObserved{NodeID: "nod_1"}); err != nil {
		t.Fatal(err)
	}
	eventually(t, 2*time.Second, "the panicked frame is acknowledged like any other", func() bool {
		return h.highestAck() >= 3
	})
}

// TestPanickingHandlerDoesNotCrashTheProcess is table-driven over the panic value shapes a real
// handler could produce (a string, an error, a runtime panic such as an out-of-range index) to
// confirm none of them escape the recovery boundary.
func TestPanickingHandlerDoesNotCrashTheProcess(t *testing.T) {
	for _, tc := range []struct {
		name    string
		trigger func(*Envelope)
	}{
		{"string panic", func(*Envelope) { panic("boom") }},
		{"error panic", func(*Envelope) { panic(errHandlerReject) }},
		{"nil deref", func(*Envelope) { var e *Envelope; _ = e.Type }},
		{"slice out of range", func(*Envelope) { var s []int; _ = s[3] }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newFakeHub(t)
			opts := testOptions(h)
			opts.Handler = HandlerFunc(func(_ context.Context, e *Envelope) error {
				tc.trigger(e)
				return nil
			})
			c := startWith(t, opts)

			eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
			h.push("dep_a")

			eventually(t, 2*time.Second, "the connection survives the panic", func() bool {
				return c.Connected() && h.connectionCount() == 1
			})
		})
	}
}

// TestPanickingResyncEndsTheSessionAndRetries is Resync's analogue: unlike a handler panic,
// Resync is not about one frame, so a recovered panic there does not get §6b's accept-and-discard
// treatment. It surfaces as an ordinary session error, ending the session and retrying with
// backoff exactly like any other resync failure — visible and bounded rather than crashing.
func TestPanickingResyncEndsTheSessionAndRetries(t *testing.T) {
	h := newFakeHub(t)
	rec := &recorder{panicSync: true}
	opts := testOptions(h)
	opts.Handler = rec
	opts.BackoffMin = time.Millisecond
	opts.BackoffMax = 5 * time.Millisecond
	c := startWith(t, opts)

	eventually(t, 2*time.Second, "first connection attempts a fresh session", func() bool {
		return h.connectionCount() >= 1
	})
	// The panic ends the session like any other resync failure: no ack is possible for a
	// resync that never finished opening its epoch, so the durable cursor's session id is what
	// the retry resumes against. The properties under test are that the goroutine survives the
	// panic and the run loop redials rather than the process going down.
	eventually(t, 3*time.Second, "the client redials after the panic instead of dying", func() bool {
		return h.connectionCount() >= 2
	})
	eventually(t, 3*time.Second, "the retried session eventually establishes", func() bool {
		return c.Connected()
	})
}

func TestCheckpointSaveFailureEndsTheSession(t *testing.T) {
	h := newFakeHub(t)
	cursor := &flakyCheckpointer{}
	rec := &recorder{}
	opts := testOptions(h)
	opts.Handler, opts.Checkpointer = rec, cursor
	startWith(t, opts)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("dep_a")
	eventually(t, 2*time.Second, "first frame applied", func() bool { return cursor.current().Seq == 1 })

	cursor.arm()
	h.push("dep_b")

	eventually(t, 3*time.Second, "session torn down after a failed checkpoint", func() bool {
		return h.connectionCount() >= 2
	})
	if got := cursor.current().Seq; got != 1 {
		t.Fatalf("cursor advanced past a frame it could not record: %d", got)
	}
}

func TestLivenessTimeoutForcesReconnect(t *testing.T) {
	h := newFakeHub(t)
	h.setSilent(true)
	startClient(t, h, func(o *Options) {
		o.HeartbeatInterval = 20 * time.Millisecond
		o.LivenessTimeout = 60 * time.Millisecond
	})

	eventually(t, 3*time.Second, "redial after a silent hub", func() bool { return h.connectionCount() >= 2 })
}

// A slow apply must not stop the reader reading.
//
// The hub pings at the WebSocket level and closes a session that has not ponged within 20 seconds,
// and the library emits that pong only from inside conn.Read. So an apply done on the read
// goroutine is a pause in the keepalive for as long as the apply lasts. In production that pause
// was the leader-election burst at startup, the hub closed the session 24 seconds in, and the
// operator's own view of the world stopped there.
func TestASlowApplyStillAnswersTheHubsKeepalive(t *testing.T) {
	h := newFakeHub(t)
	stall := newStallingHandler()

	opts := testOptions(h)
	opts.Handler = stall
	// Long enough that the client's own liveness watchdog is not what this test measures.
	opts.LivenessTimeout = 30 * time.Second
	startWith(t, opts)
	// Released after the client's own cleanup is registered, so it is unblocked before the client
	// is asked to stop rather than after.
	t.Cleanup(stall.release)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("d1")

	select {
	case <-stall.entered:
	case <-time.After(2 * time.Second):
		t.Fatal("the handler was never called")
	}

	if err := h.keepalive(2 * time.Second); err != nil {
		t.Fatalf("the hub's keepalive went unanswered while a frame was applying: %v", err)
	}
}

// A handler that never returns must not take the reconnect down with it.
//
// The session's goroutines used to be joined before the session could report anything, so a handler
// blocked on an unresponsive API server held up the error, the log line, the backoff, the redial,
// and the deferred flag that readiness is read from — all at once. The pod stayed Ready and the
// operator stayed silently disconnected for seven minutes. This handler ignores its context, which
// is the shape that caused it: a call that does not honour cancellation cannot be waited out.
func TestAWedgedApplyStillRedials(t *testing.T) {
	h := newFakeHub(t)
	stall := newStallingHandler()

	opts := testOptions(h)
	opts.Handler = stall
	startWith(t, opts)
	t.Cleanup(stall.release)

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	h.push("d1")

	select {
	case <-stall.entered:
	case <-time.After(2 * time.Second):
		t.Fatal("the handler was never called")
	}

	// The hub stops answering, so the liveness watchdog is what ends this session. Nothing else
	// can: the apply is not coming back.
	h.setSilent(true)
	eventually(t, 10*time.Second, "a redial past a wedged apply", func() bool { return h.connectionCount() >= 2 })
}

// stallingHandler blocks in Handle until released, and ignores its context while it does — a
// reconcile against an API server that has stopped answering, which is what the read path used to
// be exposed to.
type stallingHandler struct {
	entered   chan struct{}
	released  chan struct{}
	closeOnce stdsync.Once
}

func newStallingHandler() *stallingHandler {
	return &stallingHandler{entered: make(chan struct{}, 1), released: make(chan struct{})}
}

func (s *stallingHandler) Handle(context.Context, *Envelope) error {
	select {
	case s.entered <- struct{}{}:
	default:
	}
	<-s.released
	return nil
}

func (s *stallingHandler) Resync(context.Context) error { return nil }

func (s *stallingHandler) release() { s.closeOnce.Do(func() { close(s.released) }) }

// TestLossyHelloAfterReplayOverflow: once the buffer has discarded an unacknowledged frame this
// side can no longer reproduce its own stream, and PROTOCOL §4 says it must say so rather than
// ask for a resume it cannot honour.
func TestLossyHelloAfterReplayOverflow(t *testing.T) {
	h := newFakeHub(t)
	h.setWithholdAck(true)
	c, _ := startClient(t, h, func(o *Options) { o.SendQueueSize = 2 })

	eventually(t, 2*time.Second, "first connection", func() bool { return h.connectionCount() == 1 })
	for i := 0; i < 6; i++ {
		if err := c.Send("node.observed", "nod_"+string(rune('a'+i)), wire.NodeObserved{NodeID: "nod"}); err != nil {
			t.Fatal(err)
		}
		time.Sleep(10 * time.Millisecond)
	}
	eventually(t, 2*time.Second, "replay buffer overflow", func() bool { return c.replay.Lossy() })

	h.kill()
	eventually(t, 3*time.Second, "lossy hello", func() bool { return h.lastHello().Lossy })
	eventually(t, 3*time.Second, "fresh session granted", func() bool { return c.Connected() })
	if c.replay.Lossy() {
		t.Fatal("a fresh session must clear the lossy marker")
	}
}

func TestBackpressureReplacesTheSameSubject(t *testing.T) {
	q := newSendQueue(2)
	for i := 1; i <= 4; i++ {
		e := mustEnvelope(t, "device.observed", wire.DeviceObserved{DeviceID: "dev_1", Index: i})
		if err := q.Push(&outbound{env: e, subject: "dev_1"}); err != nil {
			t.Fatal(err)
		}
	}
	if q.Len() != 2 {
		t.Fatalf("a repeated subject must replace rather than grow the queue: %d", q.Len())
	}
	other := mustEnvelope(t, "device.observed", wire.DeviceObserved{DeviceID: "dev_2"})
	if err := q.Push(&outbound{env: other, subject: "dev_2"}); err != nil {
		t.Fatal(err)
	}
	if q.Dropped() != 0 {
		t.Fatalf("an observation of another subject must never evict one, got %d drops", q.Dropped())
	}
	seen := map[string]int{}
	for q.Len() > 0 {
		item, err := q.Pop(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		seen[item.subject]++
	}
	if seen["dev_1"] != 2 || seen["dev_2"] != 1 {
		t.Fatalf("every subject must survive: %v", seen)
	}
}

// TestBackpressureNeverEvictsAStreamOrAReport fills the queue past its soft bound with the frames
// that used to be evicted from the head — a stream's opener and terminator, its correlated frames,
// and an operation's terminal report — and checks every one of them is still sent, in order.
func TestBackpressureNeverEvictsAStreamOrAReport(t *testing.T) {
	q := newSendQueue(2)
	push := func(typ MessageType, subject, replyTo string) {
		t.Helper()
		e := mustEnvelope(t, typ, wire.NodeObserved{NodeID: subject})
		if replyTo != "" {
			e.ReplyTo = &replyTo
		}
		if err := q.Push(&outbound{env: e, subject: subject}); err != nil {
			t.Fatal(err)
		}
	}
	push(TypeResyncBegin, "", "")
	push("node.observed", "nod_1", "req")
	push("node.observed", "nod_2", "req")
	// An uncorrelated reading of a subject the stream already carries must not take the
	// stream's frame: the stream declared its count.
	push("node.observed", "nod_1", "")
	push(TypeStreamEnd, "", "req")
	push("operation.observed", "opr_1", "")

	var got []string
	for q.Len() > 0 {
		item, err := q.Pop(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		tag := item.env.Type + ":" + item.subject
		if item.env.ReplyTo != nil {
			tag += "@" + *item.env.ReplyTo
		}
		got = append(got, tag)
	}
	want := []string{
		"resync.begin:", "node.observed:nod_1@req", "node.observed:nod_2@req",
		"node.observed:nod_1", "stream.end:@req", "operation.observed:opr_1",
	}
	if strings.Join(got, " ") != strings.Join(want, " ") {
		t.Fatalf("queue lost or reordered frames:\n got %v\nwant %v", got, want)
	}
	if q.Dropped() != 0 {
		t.Fatalf("nothing here may be dropped, got %d", q.Dropped())
	}
}

func TestBackpressureHardLimitEndsTheSession(t *testing.T) {
	q := newSendQueue(1)
	q.limit = 3
	for i := 0; i < 3; i++ {
		e := mustEnvelope(t, "node.observed", wire.NodeObserved{})
		e.ReplyTo = replyTo("req")
		if err := q.Push(&outbound{env: e, subject: string(rune('a' + i))}); err != nil {
			t.Fatal(err)
		}
	}
	e := mustEnvelope(t, TypeStreamEnd, wire.StreamEnd{})
	if err := q.Push(&outbound{env: e}); !errors.Is(err, ErrQueueOverflow) {
		t.Fatalf("a push past the hard limit must fail, got %v", err)
	}
	if _, err := q.Pop(context.Background()); !errors.Is(err, ErrQueueOverflow) {
		t.Fatalf("the writer must be told, got %v", err)
	}
	if _, err := q.Pop(context.Background()); err != nil {
		t.Fatalf("the queued frames stay for the next session, got %v", err)
	}
}

func TestBackpressureDropsEventsFromTheTail(t *testing.T) {
	q := newSendQueue(2)
	for i := 1; i <= 4; i++ {
		e := mustEnvelope(t, "device.event", wire.DeviceEvent{DeviceID: "dev_1", Event: "degraded"})
		if err := q.Push(&outbound{env: e, subject: "dev_1"}); err != nil {
			t.Fatal(err)
		}
	}
	if q.Len() != 2 {
		t.Fatalf("queue exceeded its bound: %d", q.Len())
	}
	if q.Dropped() != 2 {
		t.Fatalf("event loss must be counted, got %d", q.Dropped())
	}
}

func TestSendNeverBlocksWhileDisconnected(t *testing.T) {
	h := newFakeHub(t)
	h.setStallHello(true)
	c, _ := startClient(t, h, func(o *Options) { o.SendQueueSize = 4 })

	for i := 0; i < 100; i++ {
		if err := c.Send("device.observed", "dev_1", wire.DeviceObserved{DeviceID: "dev_1", Index: i}); err != nil {
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
		{"deployment.desired", true}, {"node.observed", true}, {TypeError, true},
		{TypeStreamEnd, true}, {TypeResyncBegin, true},
		{TypePing, false}, {TypePong, false}, {TypeAck, false},
		{TypeHello, false}, {TypeWelcome, false},
	} {
		if got := (&Envelope{Type: tc.typ}).Sequenced(); got != tc.want {
			t.Errorf("%s: Sequenced() = %v, want %v", tc.typ, got, tc.want)
		}
	}
}

func TestNewEnvelopeIsContractShaped(t *testing.T) {
	e := mustEnvelope(t, TypePing, wire.Ping{})
	if e.V != wire.EnvelopeVersion {
		t.Errorf("v = %d, want %d", e.V, wire.EnvelopeVersion)
	}
	if len(e.ID) != 26 {
		t.Errorf("id %q is not a 26-character ULID", e.ID)
	}
	if !wire.TimestampPattern.MatchString(e.Ts) {
		t.Errorf("ts %q is not RFC 3339 UTC with milliseconds", e.Ts)
	}
	if e.Seq != nil {
		t.Error("an unsequenced frame must not carry seq")
	}
	if string(e.Payload) != "{}" {
		t.Errorf("payload must be an object, got %s", e.Payload)
	}
}
