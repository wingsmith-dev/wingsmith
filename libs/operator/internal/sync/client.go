package sync

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	mrand "math/rand/v2"
	"net/http"
	"runtime/debug"
	stdsync "sync"
	"time"

	"github.com/coder/websocket"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// maxFrameBytes caps a single inbound frame. Domain objects are small; anything larger is a bug
// or an attack, and the data plane never travels this path.
const maxFrameBytes = 8 << 20

// handshakeTimeout is PROTOCOL §1: a connection that has not completed the handshake within 10
// seconds is closed.
const handshakeTimeout = 10 * time.Second

// applyQueueDepth is how many accepted frames may be waiting on the handler before the reader
// feels it.
//
// It absorbs a burst — a snapshot stream is the shape that arrives faster than it applies — without
// letting a permanently stuck handler buffer the whole stream into memory. Slowness that outlasts
// this buffer reaches the reader, and from there the liveness timeout, which is the intended path.
const applyQueueDepth = 256

// applyTimeout bounds one call into the handler, and checkpointTimeout one durable cursor write.
//
// Both exist because the session context has no deadline of its own, and both are generous: the
// point is not to catch a slow apply but to make sure an indefinite one becomes an error somebody
// can read.
const (
	applyTimeout      = 60 * time.Second
	checkpointTimeout = 30 * time.Second
)

// loopDrainTimeout is how long a finished session waits for its own goroutines before redialling
// without them.
const loopDrainTimeout = 5 * time.Second

// Handler consumes frames the hub pushes down.
//
// Returning an error means the frame was not durably applied: the client will not advance its
// acknowledged sequence past it, so the hub replays it on the next resume. Withholding the ack is
// the only "could not apply" signal in the protocol — there is no negative acknowledgement.
type Handler interface {
	Handle(ctx context.Context, e *Envelope) error
	// Resync is called when the hub could not honour a resume, before the snapshot streams, so
	// that the receiver can open its epoch and stop treating its local view as authoritative.
	Resync(ctx context.Context) error
}

// HandlerFunc adapts a function to Handler with a no-op Resync.
type HandlerFunc func(ctx context.Context, e *Envelope) error

// Handle implements Handler.
func (f HandlerFunc) Handle(ctx context.Context, e *Envelope) error { return f(ctx, e) }

// Resync implements Handler.
func (f HandlerFunc) Resync(context.Context) error { return nil }

// Cursor is the durable resume position: which session it counts within, how far the operator has
// contiguously and durably processed, and whether this side can still reproduce its own stream.
//
// Lossy travels with the cursor rather than beside it because it is the answer to the same
// question: a lossy cursor is not a resume point, and the two must never be read apart.
type Cursor struct {
	SessionID string
	Seq       uint64
	Lossy     bool
}

// Checkpointer persists the resume cursor across operator restarts.
//
// PROTOCOL §4 requires the cursor to be durable: an operator that restarts during a partition and
// comes back with an in-memory cursor rewinds its own stream.
type Checkpointer interface {
	Load(ctx context.Context) (Cursor, error)
	Save(ctx context.Context, c Cursor) error
}

// MemoryCheckpointer keeps the resume cursor in memory only. It is for tests; a real operator
// stores the cursor beside its CRDs.
type MemoryCheckpointer struct {
	mu     stdsync.Mutex
	cursor Cursor
}

// Load implements Checkpointer.
func (m *MemoryCheckpointer) Load(context.Context) (Cursor, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.cursor, nil
}

// Save implements Checkpointer.
func (m *MemoryCheckpointer) Save(_ context.Context, c Cursor) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.cursor = c
	return nil
}

// Options configures a Client.
type Options struct {
	HubURL          string
	ClusterID       string
	Token           string
	OperatorVersion string

	TLSConfig  *tls.Config
	HTTPClient *http.Client

	HeartbeatInterval time.Duration
	LivenessTimeout   time.Duration
	BackoffMin        time.Duration
	BackoffMax        time.Duration

	SendQueueSize int

	Handler      Handler
	Checkpointer Checkpointer
	Logger       *slog.Logger
}

func (o *Options) defaults() {
	if o.HeartbeatInterval <= 0 {
		o.HeartbeatInterval = 10 * time.Second
	}
	if o.LivenessTimeout <= 0 {
		o.LivenessTimeout = 30 * time.Second
	}
	if o.BackoffMin <= 0 {
		o.BackoffMin = time.Second
	}
	if o.BackoffMax <= 0 {
		o.BackoffMax = 30 * time.Second
	}
	if o.SendQueueSize <= 0 {
		o.SendQueueSize = 1024
	}
	if o.OperatorVersion == "" {
		o.OperatorVersion = "dev"
	}
	if o.Logger == nil {
		o.Logger = slog.Default()
	}
	if o.Checkpointer == nil {
		o.Checkpointer = &MemoryCheckpointer{}
	}
	if o.Handler == nil {
		o.Handler = HandlerFunc(func(context.Context, *Envelope) error { return nil })
	}
}

// Client is the operator's outbound stream to the hub. It dials out, never listens, and owns the
// full connection lifecycle: reconnect with jittered backoff, application-level heartbeat with a
// declared liveness timeout, sequence-based resume from a durable cursor, and a bounded send
// queue.
type Client struct {
	opts Options
	log  *slog.Logger

	queue  *sendQueue
	replay *replayBuffer

	mu         stdsync.Mutex
	nextSeq    uint64
	cursor     Cursor
	expected   uint64
	holed      bool
	connected  bool
	connects   uint64
	lastRecvAt time.Time
}

// NewClient builds a client. It does not dial; call Run.
func NewClient(opts Options) *Client {
	opts.defaults()
	return &Client{
		opts:    opts,
		log:     opts.Logger.With("component", "sync"),
		queue:   newSendQueue(opts.SendQueueSize),
		replay:  newReplayBuffer(opts.SendQueueSize),
		nextSeq: 1,
	}
}

// Send enqueues a frame for delivery. It never blocks: the bounded queue's §6 policy decides what
// happens when the hub is slow or gone.
//
// The subject names the object the frame describes — a device id, a deployment id — and is what
// the queue replaces on. Two frames of one type about one subject are two readings of the same
// thing, so under pressure only the newer survives.
//
// Sequence numbers are assigned by the writer, not here, so a frame the queue replaces never
// leaves a hole in the sequence the hub would have to treat as a gap.
func (c *Client) Send(t MessageType, subject string, payload any) error {
	_, err := c.SendCorrelated(t, subject, payload, "")
	return err
}

// SendCorrelated enqueues a frame that answers, or belongs to, an earlier one, and returns the id
// the frame was given.
//
// A stream is the only reason this exists: `reply_to` is the sole correlation between a
// `resync.begin` and the `stream.end` that closes it (MESSAGES.md, Resync), so the operator cannot
// open a resync of its own without both setting reply_to and knowing the id it must be set to. An
// empty replyTo leaves the field null, which is what an ordinary event carries.
func (c *Client) SendCorrelated(t MessageType, subject string, payload any, replyTo string) (string, error) {
	e, err := newEnvelope(t, payload)
	if err != nil {
		return "", fmt.Errorf("marshal payload: %w", err)
	}
	if replyTo != "" {
		e.ReplyTo = &replyTo
	}
	return e.ID, c.queue.Push(&outbound{env: e, subject: subject})
}

// Connected reports whether a hub session is currently established.
func (c *Client) Connected() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.connected
}

// Connects reports how many hub sessions have been established, including the current one.
func (c *Client) Connects() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.connects
}

// QueueDepth reports the current outbound queue depth.
func (c *Client) QueueDepth() int { return c.queue.Len() }

// Dropped reports how many outbound events the overflow policy has discarded.
func (c *Client) Dropped() uint64 { return c.queue.Dropped() }

// Run dials the hub and keeps the session alive until ctx is cancelled. Every session failure is
// retried with exponential backoff and full jitter; there is no retry cap, because an operator
// that cannot reach the hub keeps reconciling from its CRDs indefinitely. Only a fatal protocol
// error stops the loop.
func (c *Client) Run(ctx context.Context) error {
	cursor, err := c.opts.Checkpointer.Load(ctx)
	if err != nil {
		return fmt.Errorf("load resume cursor: %w", err)
	}
	c.mu.Lock()
	c.cursor = cursor
	c.mu.Unlock()

	defer c.queue.Close()

	attempt := 0
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		err := c.session(ctx)
		switch {
		case err == nil, errors.Is(err, context.Canceled):
			if ctx.Err() != nil {
				return nil
			}
			attempt = 0
		default:
			var fatal *fatalError
			if errors.As(err, &fatal) {
				return fatal
			}
			attempt++
			c.log.Warn("hub session ended", "error", err, "attempt", attempt)
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(c.backoff(attempt)):
		}
	}
}

// backoff is exponential from BackoffMin to BackoffMax with full jitter.
func (c *Client) backoff(attempt int) time.Duration {
	if attempt <= 0 {
		return 0
	}
	d := float64(c.opts.BackoffMin) * math.Pow(2, float64(attempt-1))
	if d > float64(c.opts.BackoffMax) || math.IsInf(d, 0) {
		d = float64(c.opts.BackoffMax)
	}
	return time.Duration(mrand.Float64() * d)
}

// session runs exactly one connection: dial, handshake, then reader, writer and heartbeat until
// any of them fails.
func (c *Client) session(ctx context.Context) error {
	dialCtx, cancelDial := context.WithTimeout(ctx, 30*time.Second)
	defer cancelDial()

	httpClient := c.opts.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Transport: &http.Transport{TLSClientConfig: c.opts.TLSConfig}}
	}
	header := http.Header{}
	if c.opts.Token != "" {
		header.Set("Authorization", "Bearer "+c.opts.Token)
	}

	conn, resp, err := websocket.Dial(dialCtx, c.opts.HubURL, &websocket.DialOptions{
		HTTPClient: httpClient,
		HTTPHeader: header,
	})
	if err != nil {
		if resp != nil && (resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden) {
			return &fatalError{fmt.Errorf("hub rejected bearer token: %s", resp.Status)}
		}
		return fmt.Errorf("dial hub: %w", err)
	}
	conn.SetReadLimit(maxFrameBytes)
	defer conn.CloseNow()

	sessCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	if err := c.handshake(sessCtx, conn); err != nil {
		return err
	}

	c.mu.Lock()
	c.connected = true
	c.connects++
	c.lastRecvAt = time.Now()
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		c.connected = false
		c.mu.Unlock()
	}()

	apply := make(chan inbound, applyQueueDepth)
	errCh := make(chan error, 4)
	var wg stdsync.WaitGroup
	wg.Add(4)
	go func() {
		defer wg.Done()
		errCh <- c.runProtected("read", func() error { return c.readLoop(sessCtx, conn, apply) })
	}()
	go func() {
		defer wg.Done()
		errCh <- c.runProtected("apply", func() error { return c.applyLoop(sessCtx, apply) })
	}()
	go func() {
		defer wg.Done()
		errCh <- c.runProtected("write", func() error { return c.writeLoop(sessCtx, conn) })
	}()
	go func() {
		defer wg.Done()
		errCh <- c.runProtected("heartbeat", func() error { return c.heartbeatLoop(sessCtx) })
	}()

	err = <-errCh
	cancel()
	conn.CloseNow()
	c.awaitLoops(&wg)
	if ctx.Err() != nil {
		return nil
	}
	return err
}

// awaitLoops gives the session's goroutines a bounded chance to leave before the caller redials.
//
// Bounded, and that bound is the whole point. Joining them unconditionally is what turned one slow
// apply into a seven-minute outage that logged nothing: every signal a stalled session has —
// the error, the "hub session ended" line, the backoff, the reconnect, and the deferred
// `connected = false` that readiness is read from — is downstream of this wait, so a reader stuck
// inside a Kubernetes call suppressed all of them at once while the pod went on reporting Ready.
//
// The connection is already severed by the caller, so a goroutine that outlives this cannot write
// to a live socket, and a frame it applies late is one the next resume replays from the durable
// cursor anyway.
func (c *Client) awaitLoops(wg *stdsync.WaitGroup) {
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(loopDrainTimeout):
		c.log.Warn("session goroutines outlived the connection; reconnecting without them",
			"after", loopDrainTimeout)
	}
}

// handshake sends hello from the durable cursor, waits for welcome, and replays anything the hub
// has not acknowledged.
func (c *Client) handshake(ctx context.Context, conn *websocket.Conn) error {
	ctx, cancel := context.WithTimeout(ctx, handshakeTimeout)
	defer cancel()

	c.mu.Lock()
	cursor := c.cursor
	c.mu.Unlock()

	hello := wire.Hello{
		ClusterID:       c.opts.ClusterID,
		OperatorVersion: c.opts.OperatorVersion,
		Lossy:           cursor.Lossy || c.replay.Lossy(),
	}
	if cursor.SessionID != "" {
		session := cursor.SessionID
		hello.SessionID = &session
		hello.ResumeFrom = seqPtr(cursor.Seq)
	}

	frame, err := newEnvelope(TypeHello, hello)
	if err != nil {
		return err
	}
	frame.Ack = seqPtr(cursor.Seq)
	if err := c.write(ctx, conn, frame); err != nil {
		return fmt.Errorf("send hello: %w", err)
	}

	in, err := readEnvelope(ctx, conn)
	if err != nil {
		return fmt.Errorf("await welcome: %w", err)
	}
	switch in.Type {
	case TypeWelcome:
	case TypeError:
		return hubError(in)
	default:
		return fmt.Errorf("expected %s, got %s", TypeWelcome, in.Type)
	}

	var welcome wire.Welcome
	if err := json.Unmarshal(in.Payload, &welcome); err != nil {
		return fmt.Errorf("decode welcome: %w", err)
	}

	fresh := welcome.FullResync || welcome.SessionID != cursor.SessionID
	next := Cursor{SessionID: welcome.SessionID, Seq: cursor.Seq}
	if fresh {
		next.Seq = 0
	}
	if err := c.opts.Checkpointer.Save(ctx, next); err != nil {
		return fmt.Errorf("save resume cursor: %w", err)
	}

	c.mu.Lock()
	c.cursor = next
	c.expected = next.Seq + 1
	c.holed = false
	if fresh {
		c.nextSeq = 1
	}
	c.mu.Unlock()

	if fresh {
		c.replay.Reset()
		c.log.Info("hub started a new session", "session_id", welcome.SessionID,
			"full_resync", welcome.FullResync)
		if err := c.safeResync(ctx); err != nil {
			return fmt.Errorf("resync: %w", err)
		}
	}

	c.replay.Ack(welcome.AckSeq)
	pending := c.replay.Pending(welcome.AckSeq)
	if fresh {
		pending = nil
	}
	for _, p := range pending {
		if err := c.write(ctx, conn, p); err != nil {
			return fmt.Errorf("replay frame %d: %w", *p.Seq, err)
		}
	}
	c.log.Info("hub session established", "session_id", welcome.SessionID,
		"full_resync", welcome.FullResync, "replayed", len(pending), "resume_from", next.Seq+1)
	return nil
}

// inbound is one accepted frame on its way from the reader to the applier.
//
// handle is false for a hub error frame, which is logged where it arrives and then travels this
// channel for its sequence alone: it is checkpointed like any other frame and handed to no handler.
type inbound struct {
	env    *Envelope
	handle bool
}

// readLoop processes inbound frames strictly in order: a duplicate is dropped, a gap closes the
// connection rather than being accepted as a hole.
//
// It reads, orders, and answers pings, and does nothing else. Everything that touches Kubernetes —
// applying a frame, checkpointing a cursor — happens on applyLoop, because this goroutine sitting
// inside conn.Read is the only thing that makes the library emit a pong. Doing an apply inline
// stops the pongs for as long as the apply takes, and the hub closes a session that has not ponged
// within 20 seconds. That is not a hypothetical: it killed the first production session during the
// leader-election burst at startup.
func (c *Client) readLoop(ctx context.Context, conn *websocket.Conn, apply chan<- inbound) error {
	for {
		e, err := readEnvelope(ctx, conn)
		if err != nil {
			return err
		}
		c.mu.Lock()
		c.lastRecvAt = time.Now()
		c.mu.Unlock()

		if e.Ack != nil {
			c.replay.Ack(*e.Ack)
		}

		handle := true
		switch e.Type {
		case TypePing:
			pong, err := newEnvelope(TypePong, wire.Pong{})
			if err != nil {
				return err
			}
			pong.ReplyTo = replyTo(e.ID)
			if err := c.queue.Push(&outbound{env: pong, subject: e.ID}); err != nil {
				return err
			}
			continue
		case TypePong, TypeAck:
			continue
		case TypeWelcome, TypeHello:
			return fmt.Errorf("unexpected %s after the handshake", e.Type)
		case TypeError:
			c.log.Warn("hub error", "error", hubError(e))
			handle = false
		}

		// Ordering is decided here rather than at the applier so that a gap still ends the session
		// at the moment it arrives. Arrival order is what this cursor tracks, and it is cheap; the
		// applied position is the applier's, and it is not.
		fresh, err := c.accept(e)
		if err != nil {
			return err
		}
		if !fresh {
			continue
		}

		select {
		case apply <- inbound{env: e, handle: handle}:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// applyLoop hands frames to the handler and moves the durable cursor, one at a time and in the
// order the reader accepted them.
//
// It is a goroutine of its own so that a slow apply costs throughput and not the session. The
// channel in front of it is bounded, so sustained slowness still reaches the reader in the end —
// and when it does, the heartbeat's liveness timeout ends the session and the next resume replays
// from the last checkpoint, which is the recoverable direction.
func (c *Client) applyLoop(ctx context.Context, apply <-chan inbound) error {
	for {
		var in inbound
		select {
		case <-ctx.Done():
			return ctx.Err()
		case in = <-apply:
		}
		e := in.env

		if in.handle {
			if err := c.handleFrame(ctx, e); err != nil {
				var pe *panicError
				if errors.As(err, &pe) {
					// PROTOCOL §6b: a panic is not obviously transient, and a deterministic one
					// recurs on every retry — withholding the ack would wedge the stream behind it
					// forever. Treated as a permanently unappliable frame instead: accepted,
					// discarded, and logged loudly, exactly like an unknown type or a malformed
					// payload. The frame's sequence still advances below.
					c.log.Error("handler panicked; discarding frame per PROTOCOL 6b",
						"type", e.Type, "seq", e.Seq, "panic", pe.value, "stack", string(pe.stack))
				} else {
					c.log.Error("handler rejected frame; withholding ack",
						"type", e.Type, "seq", e.Seq, "error", err)
					c.mu.Lock()
					c.holed = true
					c.mu.Unlock()
					continue
				}
			}
		}

		if !e.Sequenced() {
			continue
		}
		if err := c.advance(ctx, *e.Seq); err != nil {
			return err
		}
	}
}

// handleFrame applies one frame under a deadline of its own.
//
// The deadline is here because the session context has none: a handler blocked on an unresponsive
// API server would otherwise hold this goroutine for as long as that lasts, and a stall with no
// bound is a stall nothing can report. Exceeding it is an ordinary rejection — the ack is withheld
// and the hub replays the frame on the next resume.
func (c *Client) handleFrame(ctx context.Context, e *Envelope) error {
	ctx, cancel := context.WithTimeout(ctx, applyTimeout)
	defer cancel()
	return c.safeHandle(ctx, e)
}

// accept applies §4's ordering rules to one inbound frame and reports whether it is new.
//
// Arrival order and applied position are two different cursors and must not be conflated. The
// expected sequence tracks what has arrived; the durable cursor tracks what has been applied
// contiguously. A frame the handler refuses stalls the second without stalling the first —
// withholding the ack is how "I could not apply this" is said, and it must not manufacture a gap
// out of the peer's next perfectly ordinary frame.
func (c *Client) accept(e *Envelope) (bool, error) {
	if !e.Sequenced() {
		return true, nil
	}
	if e.Seq == nil {
		return false, fmt.Errorf("sequenced frame %s carries no seq", e.Type)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	switch {
	case *e.Seq < c.expected:
		c.log.Debug("dropping duplicate frame", "seq", *e.Seq, "expected", c.expected)
		return false, nil
	case *e.Seq > c.expected:
		return false, fmt.Errorf("sequence gap: expected %d, got %d", c.expected, *e.Seq)
	}
	c.expected++
	return true, nil
}

// advance records the frame as durably processed and makes sure the acknowledgement reaches the
// hub.
//
// A checkpoint write that fails ends the session rather than the frame. Continuing would leave
// the in-memory cursor ahead of the durable one, so the next resume would ask from a point that
// was never recorded and the frames in between would never be replayed. Ending the session costs
// a reconnect and replays from the last durable point, which is the recoverable direction.
func (c *Client) advance(ctx context.Context, seq uint64) error {
	c.mu.Lock()
	next, holed := c.cursor, c.holed
	c.mu.Unlock()
	if holed || seq != next.Seq+1 {
		return nil
	}
	next.Seq = seq

	saveCtx, cancel := context.WithTimeout(ctx, checkpointTimeout)
	defer cancel()
	if err := c.opts.Checkpointer.Save(saveCtx, next); err != nil {
		return fmt.Errorf("save resume cursor at seq %d: %w", seq, err)
	}
	c.mu.Lock()
	c.cursor = next
	c.mu.Unlock()

	if c.queue.Len() > 0 {
		return nil
	}
	ack, err := newEnvelope(TypeAck, wire.Ack{})
	if err != nil {
		return err
	}
	return c.queue.Push(&outbound{env: ack, subject: ""})
}

// writeLoop is the only writer. It assigns sequence numbers here rather than at Send so that a
// frame the queue discarded never consumed one, and stamps the current ack on every frame so an
// acknowledgement rides out on whatever is leaving anyway.
func (c *Client) writeLoop(ctx context.Context, conn *websocket.Conn) error {
	for {
		item, err := c.queue.Pop(ctx)
		if err != nil {
			return err
		}
		e := item.env
		if e.Sequenced() && e.Seq == nil {
			c.mu.Lock()
			e.Seq = seqPtr(c.nextSeq)
			c.nextSeq++
			c.mu.Unlock()
			if c.replay.Add(e) {
				c.log.Warn("replay buffer overflowed; the next hello declares itself lossy")
				if err := c.markLossy(ctx); err != nil {
					return err
				}
			}
		}
		e.Ack = seqPtr(c.ackCursor())
		if err := c.write(ctx, conn, e); err != nil {
			return err
		}
	}
}

// markLossy records durably that this side can no longer reproduce its own stream.
func (c *Client) markLossy(ctx context.Context) error {
	c.mu.Lock()
	next := c.cursor
	c.mu.Unlock()
	if next.Lossy || next.SessionID == "" {
		return nil
	}
	next.Lossy = true
	if err := c.opts.Checkpointer.Save(ctx, next); err != nil {
		return fmt.Errorf("record lossy replay buffer: %w", err)
	}
	c.mu.Lock()
	c.cursor.Lossy = true
	c.mu.Unlock()
	return nil
}

// heartbeatLoop sends an application-level ping on an interval and tears the connection down when
// nothing has been received within LivenessTimeout. A TCP connection can stay open long after the
// peer has stopped making progress, so liveness is asserted at the application layer.
func (c *Client) heartbeatLoop(ctx context.Context) error {
	ticker := time.NewTicker(c.opts.HeartbeatInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			c.mu.Lock()
			idle := time.Since(c.lastRecvAt)
			c.mu.Unlock()
			if idle > c.opts.LivenessTimeout {
				return fmt.Errorf("hub silent for %s, exceeding liveness timeout %s",
					idle.Truncate(time.Millisecond), c.opts.LivenessTimeout)
			}
			ping, err := newEnvelope(TypePing, wire.Ping{})
			if err != nil {
				return err
			}
			if err := c.queue.Push(&outbound{env: ping, subject: ""}); err != nil {
				return err
			}
		}
	}
}

func (c *Client) ackCursor() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.cursor.Seq
}

func (c *Client) write(ctx context.Context, conn *websocket.Conn, e *Envelope) error {
	b, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("marshal envelope: %w", err)
	}
	wctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return conn.Write(wctx, websocket.MessageText, b)
}

// hubError renders an error frame, marking a non-retryable one fatal so the run loop stops
// instead of redialling into the same refusal.
func hubError(e *Envelope) error {
	var p wire.Error
	_ = json.Unmarshal(e.Payload, &p)
	err := fmt.Errorf("hub error %s: %s", p.Code, p.Message)
	if !p.Retryable {
		return &fatalError{err}
	}
	return err
}

func readEnvelope(ctx context.Context, conn *websocket.Conn) (*Envelope, error) {
	typ, b, err := conn.Read(ctx)
	if err != nil {
		return nil, err
	}
	if typ != websocket.MessageText {
		return nil, fmt.Errorf("unexpected frame type %v", typ)
	}
	var e Envelope
	if err := json.Unmarshal(b, &e); err != nil {
		return nil, fmt.Errorf("decode envelope: %w", err)
	}
	if e.V != wire.EnvelopeVersion {
		return nil, fmt.Errorf("unsupported envelope version %d", e.V)
	}
	return &e, nil
}

// fatalError marks a failure that retrying cannot fix, such as a rejected bearer token.
type fatalError struct{ err error }

func (f *fatalError) Error() string { return f.err.Error() }
func (f *fatalError) Unwrap() error { return f.err }

// panicError marks a failure recovered from a panic rather than returned normally, carrying the
// recovered value and a stack trace captured at the point of recovery so the log line is enough
// to diagnose without reproducing the crash.
type panicError struct {
	value any
	stack []byte
}

func (p *panicError) Error() string { return fmt.Sprintf("panic: %v", p.value) }

// safeHandle calls the handler chain with a recovery boundary. A panic there is a bug in code the
// client does not own — a nil deref, a slice out of range, a panic surfaced from the Kubernetes
// client — and must degrade one frame rather than take down the whole operator process.
func (c *Client) safeHandle(ctx context.Context, e *Envelope) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = &panicError{value: r, stack: debug.Stack()}
		}
	}()
	return c.opts.Handler.Handle(ctx, e)
}

// safeResync calls Resync with the same recovery boundary as safeHandle. Resync is not about one
// frame, so a recovered panic here is not eligible for §6b's accept-and-discard treatment; it
// surfaces as an ordinary session error, which ends the session and retries with backoff exactly
// like any other resync failure.
func (c *Client) safeResync(ctx context.Context) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = &panicError{value: r, stack: debug.Stack()}
		}
	}()
	return c.opts.Handler.Resync(ctx)
}

// runProtected recovers a panic from fn so a bug in one session goroutine ends that session
// rather than the process. The recovered error is treated like any other session error by Run:
// the session is torn down and retried with jittered backoff.
func (c *Client) runProtected(label string, fn func() error) (err error) {
	defer func() {
		if r := recover(); r != nil {
			stack := debug.Stack()
			c.log.Error("recovered panic in session goroutine",
				"goroutine", label, "panic", r, "stack", string(stack))
			err = &panicError{value: r, stack: stack}
		}
	}()
	return fn()
}
