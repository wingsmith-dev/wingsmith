package sync

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	stdsync "sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// typeDesired is the concrete hub -> operator declaration these tests drive the client with. The
// client is type-agnostic below the envelope, so one real type exercises the whole path.
const typeDesired = "deployment.desired"

// fakeHub is an in-process stand-in for the control-plane hub. It implements exactly the parts of
// PROTOCOL.md the client depends on: the bearer-token handshake, welcome with resume or a fresh
// session, a sequenced downward log with replay, and acknowledgement of the upward stream.
type fakeHub struct {
	t   *testing.T
	srv *httptest.Server

	mu          stdsync.Mutex
	sessionID   string
	allowResume bool
	rejectToken bool
	stallHello  bool
	withholdAck bool
	silent      bool
	authHeaders []string

	downward    []*Envelope
	nextDownSeq uint64

	upward      []*Envelope
	lastUpSeq   uint64
	connections int
	hellos      []wire.Hello

	conn   *websocket.Conn
	cancel context.CancelFunc
}

func newFakeHub(t *testing.T) *fakeHub {
	t.Helper()
	h := &fakeHub{t: t, sessionID: "session-1", allowResume: true, nextDownSeq: 1}
	h.srv = httptest.NewServer(http.HandlerFunc(h.serve))
	t.Cleanup(h.srv.Close)
	return h
}

func (h *fakeHub) url() string { return "ws" + strings.TrimPrefix(h.srv.URL, "http") }

func (h *fakeHub) serve(w http.ResponseWriter, r *http.Request) {
	h.mu.Lock()
	reject, stall := h.rejectToken, h.stallHello
	h.authHeaders = append(h.authHeaders, r.Header.Get("Authorization"))
	h.mu.Unlock()
	if reject {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}

	conn, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	conn.SetReadLimit(maxFrameBytes)
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	defer conn.CloseNow()

	e, err := readEnvelope(ctx, conn)
	if err != nil || e.Type != TypeHello {
		return
	}
	var hello wire.Hello
	if err := json.Unmarshal(e.Payload, &hello); err != nil {
		return
	}

	h.mu.Lock()
	h.connections++
	h.hellos = append(h.hellos, hello)
	resumed := h.allowResume && !hello.Lossy &&
		hello.SessionID != nil && *hello.SessionID == h.sessionID
	from := uint64(0)
	if resumed && hello.ResumeFrom != nil {
		from = *hello.ResumeFrom
	}
	lastUp := h.lastUpSeq
	if !resumed {
		h.sessionID += "+"
		h.lastUpSeq, lastUp = 0, 0
		h.renumberLocked()
	}
	session := h.sessionID
	backlog := make([]*Envelope, 0, len(h.downward))
	for _, d := range h.downward {
		if d.Seq != nil && *d.Seq > from {
			backlog = append(backlog, d)
		}
	}
	h.conn, h.cancel = conn, cancel
	h.mu.Unlock()

	if stall {
		<-ctx.Done()
		return
	}

	welcome := mustEnvelope(h.t, TypeWelcome, wire.Welcome{
		SessionID: session, AckSeq: lastUp, FullResync: !resumed,
	})
	welcome.ReplyTo = replyTo(e.ID)
	if err := writeJSON(ctx, conn, welcome); err != nil {
		return
	}
	for _, d := range backlog {
		if err := writeJSON(ctx, conn, d); err != nil {
			return
		}
	}

	for {
		in, err := readEnvelope(ctx, conn)
		if err != nil {
			return
		}
		h.mu.Lock()
		withhold, silent := h.withholdAck, h.silent
		if in.Sequenced() && in.Seq != nil && *in.Seq == h.lastUpSeq+1 {
			h.lastUpSeq = *in.Seq
			h.upward = append(h.upward, in)
		}
		up := h.lastUpSeq
		h.mu.Unlock()

		if silent {
			continue
		}
		switch in.Type {
		case TypePing:
			pong := mustEnvelope(h.t, TypePong, wire.Pong{})
			pong.ReplyTo = replyTo(in.ID)
			if !withhold {
				pong.Ack = seqPtr(up)
			}
			_ = writeJSON(ctx, conn, pong)
		case TypePong, TypeAck:
		default:
			if withhold {
				continue
			}
			ack := mustEnvelope(h.t, TypeAck, wire.Ack{})
			ack.ReplyTo, ack.Ack = replyTo(in.ID), seqPtr(up)
			_ = writeJSON(ctx, conn, ack)
		}
	}
}

// renumberLocked restarts the downward log at seq 1, which is what a fresh session means: the new
// stream numbers from 1 and carries the hub's complete desired state again.
func (h *fakeHub) renumberLocked() {
	h.nextDownSeq = 1
	for _, d := range h.downward {
		d.Seq = seqPtr(h.nextDownSeq)
		h.nextDownSeq++
	}
}

// push appends a declaration to the hub's downward log and delivers it if a connection is live.
func (h *fakeHub) push(id string) *Envelope {
	e := mustEnvelope(h.t, typeDesired, wire.DeploymentDesired{DeploymentID: id, Generation: 1})
	h.mu.Lock()
	e.Seq = seqPtr(h.nextDownSeq)
	h.nextDownSeq++
	h.downward = append(h.downward, e)
	conn := h.conn
	h.mu.Unlock()
	if conn != nil {
		_ = writeJSON(context.Background(), conn, e)
	}
	return e
}

// pushAt delivers a frame at an explicit sequence number without recording it in the log. It is
// how the tests produce a duplicate or a gap without corrupting the hub's own bookkeeping.
func (h *fakeHub) pushAt(seq uint64, id string) {
	e := mustEnvelope(h.t, typeDesired, wire.DeploymentDesired{DeploymentID: id, Generation: 1})
	e.Seq = seqPtr(seq)
	h.mu.Lock()
	conn := h.conn
	h.mu.Unlock()
	if conn != nil {
		_ = writeJSON(context.Background(), conn, e)
	}
}

// kill drops the live connection without a close handshake, simulating a partition.
func (h *fakeHub) kill() {
	h.mu.Lock()
	conn, cancel := h.conn, h.cancel
	h.conn, h.cancel = nil, nil
	h.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	if conn != nil {
		_ = conn.CloseNow()
	}
}

// keepalive sends a WebSocket-level ping and waits for the pong, which is what the real hub does
// every 20 seconds and closes the session over when it goes unanswered.
//
// This is a different mechanism from the application-level ping frames elsewhere in this harness,
// and the difference is the whole reason it exists: the library answers a control ping only while a
// goroutine on the other side is inside conn.Read, so this is the only way a test can tell whether
// the client's reader is actually reading.
func (h *fakeHub) keepalive(timeout time.Duration) error {
	h.mu.Lock()
	conn := h.conn
	h.mu.Unlock()
	if conn == nil {
		return errors.New("no live connection to ping")
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	return conn.Ping(ctx)
}

func (h *fakeHub) connectionCount() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.connections
}

func (h *fakeHub) lastHello() wire.Hello {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.hellos) == 0 {
		return wire.Hello{}
	}
	return h.hellos[len(h.hellos)-1]
}

func (h *fakeHub) lastAuthHeader() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.authHeaders) == 0 {
		return ""
	}
	return h.authHeaders[len(h.authHeaders)-1]
}

func (h *fakeHub) upwardSeqs() []uint64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]uint64, 0, len(h.upward))
	for _, e := range h.upward {
		if e.Seq != nil {
			out = append(out, *e.Seq)
		}
	}
	return out
}

// highestAck is the highest acknowledgement the operator has sent, which is the only signal the
// hub has that a frame was durably applied.
func (h *fakeHub) highestAck() uint64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	var best uint64
	for _, e := range h.upward {
		if e.Ack != nil && *e.Ack > best {
			best = *e.Ack
		}
	}
	return best
}

func (h *fakeHub) setResume(v bool) {
	h.mu.Lock()
	h.allowResume = v
	h.mu.Unlock()
}

func (h *fakeHub) setWithholdAck(v bool) {
	h.mu.Lock()
	h.withholdAck = v
	h.mu.Unlock()
}

// setSilent makes the hub complete the handshake and then never speak again, which is the
// failure the application-level heartbeat exists to detect: the socket is open and the peer is
// not making progress.
func (h *fakeHub) setSilent(v bool) {
	h.mu.Lock()
	h.silent = v
	h.mu.Unlock()
}

func (h *fakeHub) setStallHello(v bool) {
	h.mu.Lock()
	h.stallHello = v
	h.mu.Unlock()
}

func mustEnvelope(t *testing.T, typ MessageType, payload any) *Envelope {
	t.Helper()
	e, err := newEnvelope(typ, payload)
	if err != nil {
		t.Fatalf("build %s: %v", typ, err)
	}
	return e
}

func writeJSON(ctx context.Context, conn *websocket.Conn, e *Envelope) error {
	b, err := json.Marshal(e)
	if err != nil {
		return err
	}
	wctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	return conn.Write(wctx, websocket.MessageText, b)
}

// eventually polls cond until it holds or the deadline passes.
func eventually(t *testing.T, timeout time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

var errHandlerReject = errors.New("handler rejected")
