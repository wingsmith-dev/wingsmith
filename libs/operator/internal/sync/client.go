package sync

import (
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	mrand "math/rand/v2"
	"net/http"
	stdsync "sync"
	"time"

	"github.com/coder/websocket"
)

const (
	policyDropOldest = "drop-oldest"
	policyDropNewest = "drop-newest"
	policyDisconnect = "disconnect"
)

// maxFrameBytes caps a single inbound frame. Domain objects are small; anything larger is a bug
// or an attack, and the data plane never travels this path.
const maxFrameBytes = 8 << 20

// Handler consumes envelopes the hub pushes down.
//
// Returning an error means the envelope was not durably applied: the client will not advance its
// acknowledged sequence past it, so the hub replays it on the next resume. This is the seam where
// the sync loop hands desired state to the CRD writer.
type Handler interface {
	Handle(ctx context.Context, e *Envelope) error
	// Resync is called when the hub could not honour a resume and the local view must be
	// rebuilt from the replayed stream rather than trusted incrementally.
	Resync(ctx context.Context) error
}

// HandlerFunc adapts a function to Handler with a no-op Resync.
type HandlerFunc func(ctx context.Context, e *Envelope) error

// Handle implements Handler.
func (f HandlerFunc) Handle(ctx context.Context, e *Envelope) error { return f(ctx, e) }

// Resync implements Handler.
func (f HandlerFunc) Resync(context.Context) error { return nil }

// Checkpointer persists the resume cursor across operator restarts.
//
// The in-memory implementation is fine for a skeleton, but a restart during a partition must not
// silently rewind the stream, so the real implementation is expected to store the cursor
// alongside the CRDs that hold desired state.
type Checkpointer interface {
	Load(ctx context.Context) (sessionID string, lastReceivedSeq uint64, err error)
	Save(ctx context.Context, sessionID string, lastReceivedSeq uint64) error
}

// MemoryCheckpointer keeps the resume cursor in memory only.
type MemoryCheckpointer struct {
	mu      stdsync.Mutex
	session string
	seq     uint64
}

// Load implements Checkpointer.
func (m *MemoryCheckpointer) Load(context.Context) (string, uint64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.session, m.seq, nil
}

// Save implements Checkpointer.
func (m *MemoryCheckpointer) Save(_ context.Context, session string, seq uint64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.session, m.seq = session, seq
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

	SendQueueSize   int
	SendQueuePolicy string

	Handler      Handler
	Checkpointer Checkpointer
	Logger       *slog.Logger
}

func (o *Options) defaults() {
	if o.HeartbeatInterval <= 0 {
		o.HeartbeatInterval = 10 * time.Second
	}
	if o.LivenessTimeout <= 0 {
		o.LivenessTimeout = 3 * o.HeartbeatInterval
	}
	if o.BackoffMin <= 0 {
		o.BackoffMin = 500 * time.Millisecond
	}
	if o.BackoffMax <= 0 {
		o.BackoffMax = 30 * time.Second
	}
	if o.SendQueueSize <= 0 {
		o.SendQueueSize = 1024
	}
	if o.SendQueuePolicy == "" {
		o.SendQueuePolicy = policyDropOldest
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
// declared liveness timeout, sequence-based resume, and a bounded send queue.
type Client struct {
	opts Options
	log  *slog.Logger

	queue  *sendQueue
	replay *replayBuffer

	mu              stdsync.Mutex
	nextSeq         uint64
	lastReceivedSeq uint64
	sessionID       string
	connected       bool
	connects        uint64
	lastRecvAt      time.Time
}

// NewClient builds a client. It does not dial; call Run.
func NewClient(opts Options) *Client {
	opts.defaults()
	return &Client{
		opts:    opts,
		log:     opts.Logger.With("component", "sync"),
		queue:   newSendQueue(opts.SendQueueSize, opts.SendQueuePolicy),
		replay:  newReplayBuffer(opts.SendQueueSize),
		nextSeq: 1,
	}
}

// Send enqueues a sequenced envelope for delivery. It never blocks: the bounded queue's overflow
// policy decides what happens when the hub is slow or gone.
//
// A sequenced envelope enters the replay buffer before it enters the send queue, so an envelope
// the overflow policy drops from the live path is still resent on the next resume. Backpressure
// therefore costs latency and ordering, not delivery, until the replay buffer itself overflows —
// at which point the buffer marks itself lossy and the next handshake asks for a fresh session.
func (c *Client) Send(t MessageType, payload any) error {
	var raw json.RawMessage
	if payload != nil {
		b, err := json.Marshal(payload)
		if err != nil {
			return fmt.Errorf("marshal payload: %w", err)
		}
		raw = b
	}
	e := &Envelope{Type: t, ID: newID(), Timestamp: time.Now().UTC(), Payload: raw}
	if e.Sequenced() {
		c.mu.Lock()
		e.Seq = c.nextSeq
		c.nextSeq++
		c.mu.Unlock()
		c.replay.Add(e)
	}
	return c.queue.Push(e)
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

// Dropped reports how many outbound envelopes the overflow policy has discarded.
func (c *Client) Dropped() uint64 { return c.queue.Dropped() }

// Run dials the hub and keeps the session alive until ctx is cancelled. Every session failure is
// retried with exponential backoff and full jitter; only a fatal protocol error stops the loop.
func (c *Client) Run(ctx context.Context) error {
	session, seq, err := c.opts.Checkpointer.Load(ctx)
	if err != nil {
		return fmt.Errorf("load resume cursor: %w", err)
	}
	c.mu.Lock()
	c.sessionID, c.lastReceivedSeq = session, seq
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
		d := c.backoff(attempt)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(d):
		}
	}
}

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
	header.Set("X-Hangar-Cluster-Id", c.opts.ClusterID)

	conn, resp, err := websocket.Dial(dialCtx, c.opts.HubURL, &websocket.DialOptions{
		HTTPClient: httpClient,
		HTTPHeader: header,
	})
	if err != nil {
		if resp != nil && (resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden) {
			return &fatalError{fmt.Errorf("hub rejected enrolment token: %s", resp.Status)}
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

	errCh := make(chan error, 3)
	var wg stdsync.WaitGroup
	wg.Add(3)
	go func() { defer wg.Done(); errCh <- c.readLoop(sessCtx, conn) }()
	go func() { defer wg.Done(); errCh <- c.writeLoop(sessCtx, conn) }()
	go func() { defer wg.Done(); errCh <- c.heartbeatLoop(sessCtx) }()

	err = <-errCh
	cancel()
	conn.CloseNow()
	wg.Wait()
	if ctx.Err() != nil {
		return nil
	}
	return err
}

// handshake sends hello, waits for hello.ack, and replays anything the hub has not acknowledged.
func (c *Client) handshake(ctx context.Context, conn *websocket.Conn) error {
	c.mu.Lock()
	hello := Hello{
		ClusterID:       c.opts.ClusterID,
		Token:           c.opts.Token,
		OperatorVersion: c.opts.OperatorVersion,
		SessionID:       c.sessionID,
		LastReceivedSeq: c.lastReceivedSeq,
		NextSendSeq:     c.nextSeq,
	}
	c.mu.Unlock()
	if c.replay.Lossy() {
		hello.SessionID = ""
	}

	if err := c.writeEnvelope(ctx, conn, &Envelope{
		Type:      TypeHello,
		ID:        newID(),
		Timestamp: time.Now().UTC(),
		Ack:       hello.LastReceivedSeq,
		Payload:   mustJSON(hello),
	}); err != nil {
		return fmt.Errorf("send hello: %w", err)
	}

	ackCtx, cancel := context.WithTimeout(ctx, c.opts.LivenessTimeout)
	defer cancel()
	e, err := readEnvelope(ackCtx, conn)
	if err != nil {
		return fmt.Errorf("await hello.ack: %w", err)
	}
	if e.Type == TypeError {
		var p ErrorPayload
		_ = json.Unmarshal(e.Payload, &p)
		if p.Fatal {
			return &fatalError{fmt.Errorf("hub refused connection: %s: %s", p.Code, p.Message)}
		}
		return fmt.Errorf("hub error: %s: %s", p.Code, p.Message)
	}
	if e.Type != TypeHelloAck {
		return fmt.Errorf("expected %s, got %s", TypeHelloAck, e.Type)
	}
	var ack HelloAck
	if err := json.Unmarshal(e.Payload, &ack); err != nil {
		return fmt.Errorf("decode hello.ack: %w", err)
	}

	c.mu.Lock()
	previous := c.sessionID
	c.sessionID = ack.SessionID
	if !ack.Resumed || previous != ack.SessionID {
		c.lastReceivedSeq = 0
	}
	last := c.lastReceivedSeq
	c.mu.Unlock()

	if err := c.opts.Checkpointer.Save(ctx, ack.SessionID, last); err != nil {
		return fmt.Errorf("save resume cursor: %w", err)
	}

	if !ack.Resumed {
		c.log.Info("hub started a new session", "session_id", ack.SessionID)
		if previous != "" {
			if err := c.opts.Handler.Resync(ctx); err != nil {
				return fmt.Errorf("resync: %w", err)
			}
		}
	}

	c.replay.Ack(ack.LastReceivedSeq)
	pending := c.replay.Pending(ack.LastReceivedSeq)
	for _, p := range pending {
		if err := c.writeEnvelope(ctx, conn, p); err != nil {
			return fmt.Errorf("replay envelope %d: %w", p.Seq, err)
		}
	}
	c.log.Info("hub session established",
		"session_id", ack.SessionID, "resumed", ack.Resumed,
		"replayed", len(pending), "resume_from", last+1)
	return nil
}

func (c *Client) readLoop(ctx context.Context, conn *websocket.Conn) error {
	for {
		e, err := readEnvelope(ctx, conn)
		if err != nil {
			return err
		}
		c.mu.Lock()
		c.lastRecvAt = time.Now()
		c.mu.Unlock()

		if e.Ack > 0 {
			c.replay.Ack(e.Ack)
		}

		switch e.Type {
		case TypePing:
			if err := c.queue.Push(&Envelope{
				Type: TypePong, ID: newID(), CorrelationID: e.ID,
				Timestamp: time.Now().UTC(), Ack: c.ackCursor(),
			}); err != nil {
				return err
			}
			continue
		case TypePong, TypeAck:
			continue
		case TypeError:
			var p ErrorPayload
			_ = json.Unmarshal(e.Payload, &p)
			if p.Fatal {
				return &fatalError{fmt.Errorf("hub error: %s: %s", p.Code, p.Message)}
			}
			c.log.Warn("hub error", "code", p.Code, "message", p.Message)
			continue
		}

		if e.Sequenced() {
			c.mu.Lock()
			expected := c.lastReceivedSeq + 1
			c.mu.Unlock()
			if e.Seq < expected {
				c.log.Debug("dropping duplicate envelope", "seq", e.Seq, "expected", expected)
				continue
			}
			if e.Seq > expected {
				return fmt.Errorf("sequence gap: expected %d, got %d", expected, e.Seq)
			}
		}

		if err := c.opts.Handler.Handle(ctx, e); err != nil {
			c.log.Error("handler rejected envelope", "type", e.Type, "seq", e.Seq, "error", err)
			continue
		}

		if e.Sequenced() {
			c.mu.Lock()
			c.lastReceivedSeq = e.Seq
			session, last := c.sessionID, c.lastReceivedSeq
			c.mu.Unlock()
			if err := c.opts.Checkpointer.Save(ctx, session, last); err != nil {
				return fmt.Errorf("save resume cursor: %w", err)
			}
			if err := c.queue.Push(&Envelope{
				Type: TypeAck, ID: newID(), CorrelationID: e.ID,
				Timestamp: time.Now().UTC(), Ack: last,
			}); err != nil {
				return err
			}
		}
	}
}

func (c *Client) writeLoop(ctx context.Context, conn *websocket.Conn) error {
	for {
		e, err := c.queue.Pop(ctx)
		if err != nil {
			return err
		}
		if e.Ack == 0 {
			e.Ack = c.ackCursor()
		}
		if err := c.writeEnvelope(ctx, conn, e); err != nil {
			return err
		}
	}
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
				return fmt.Errorf("hub silent for %s, exceeding liveness timeout %s", idle.Truncate(time.Millisecond), c.opts.LivenessTimeout)
			}
			if err := c.queue.Push(&Envelope{
				Type: TypePing, ID: newID(), Timestamp: time.Now().UTC(), Ack: c.ackCursor(),
			}); err != nil {
				return err
			}
		}
	}
}

func (c *Client) ackCursor() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.lastReceivedSeq
}

func (c *Client) writeEnvelope(ctx context.Context, conn *websocket.Conn, e *Envelope) error {
	b, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("marshal envelope: %w", err)
	}
	wctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return conn.Write(wctx, websocket.MessageText, b)
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
	return &e, nil
}

// fatalError marks a failure that retrying cannot fix, such as a rejected enrolment token.
type fatalError struct{ err error }

func (f *fatalError) Error() string { return f.err.Error() }
func (f *fatalError) Unwrap() error { return f.err }

func newID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

func mustJSON(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}
