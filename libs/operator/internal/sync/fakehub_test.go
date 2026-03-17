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
)

// fakeHub is an in-process stand-in for the control-plane hub. It implements exactly the parts of
// the protocol the client depends on: the hello handshake, a sequenced downward log with replay,
// and acknowledgement of the upward stream.
type fakeHub struct {
	t   *testing.T
	srv *httptest.Server

	mu          stdsync.Mutex
	sessionID   string
	allowResume bool
	rejectToken bool
	stallHello  bool

	downward    []*Envelope
	nextDownSeq uint64

	upward      []*Envelope
	lastUpSeq   uint64
	connections int
	hellos      []Hello

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
	var hello Hello
	if err := json.Unmarshal(e.Payload, &hello); err != nil {
		return
	}

	h.mu.Lock()
	h.connections++
	h.hellos = append(h.hellos, hello)
	resumed := h.allowResume && hello.SessionID == h.sessionID
	if !resumed {
		h.sessionID = h.sessionID + "+"
	}
	session := h.sessionID
	lastUp := h.lastUpSeq
	from := hello.LastReceivedSeq
	if !resumed {
		from = 0
		lastUp = 0
		h.lastUpSeq = 0
	}
	backlog := make([]*Envelope, 0, len(h.downward))
	for _, d := range h.downward {
		if d.Seq > from {
			backlog = append(backlog, d)
		}
	}
	h.conn, h.cancel = conn, cancel
	h.mu.Unlock()

	if stall {
		<-ctx.Done()
		return
	}

	ack := HelloAck{SessionID: session, Resumed: resumed, LastReceivedSeq: lastUp}
	if err := writeJSON(ctx, conn, &Envelope{
		Type: TypeHelloAck, ID: "hello-ack", CorrelationID: e.ID,
		Timestamp: time.Now().UTC(), Payload: mustJSON(ack),
	}); err != nil {
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
		if in.Sequenced() && in.Seq == h.lastUpSeq+1 {
			h.lastUpSeq = in.Seq
			h.upward = append(h.upward, in)
		}
		up := h.lastUpSeq
		h.mu.Unlock()

		switch in.Type {
		case TypePing:
			_ = writeJSON(ctx, conn, &Envelope{Type: TypePong, ID: "pong", CorrelationID: in.ID, Timestamp: time.Now().UTC(), Ack: up})
		case TypeAck, TypePong:
		default:
			_ = writeJSON(ctx, conn, &Envelope{Type: TypeAck, ID: "ack", CorrelationID: in.ID, Timestamp: time.Now().UTC(), Ack: up})
		}
	}
}

// push appends an envelope to the hub's downward log and delivers it if a connection is live.
func (h *fakeHub) push(payload any) *Envelope {
	h.mu.Lock()
	e := &Envelope{
		Type: TypeDesired, ID: "down", Seq: h.nextDownSeq,
		Timestamp: time.Now().UTC(), Payload: mustJSON(payload),
	}
	h.nextDownSeq++
	h.downward = append(h.downward, e)
	conn := h.conn
	h.mu.Unlock()
	if conn != nil {
		_ = writeJSON(context.Background(), conn, e)
	}
	return e
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

func (h *fakeHub) connectionCount() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.connections
}

func (h *fakeHub) lastHello() Hello {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.hellos) == 0 {
		return Hello{}
	}
	return h.hellos[len(h.hellos)-1]
}

func (h *fakeHub) upwardSeqs() []uint64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]uint64, 0, len(h.upward))
	for _, e := range h.upward {
		out = append(out, e.Seq)
	}
	return out
}

func (h *fakeHub) setResume(v bool) {
	h.mu.Lock()
	h.allowResume = v
	h.mu.Unlock()
}

func (h *fakeHub) setStallHello(v bool) {
	h.mu.Lock()
	h.stallHello = v
	h.mu.Unlock()
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
