package tunnel

import (
	"bytes"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"sync"
	"testing"
	"time"

	"golang.org/x/net/http2"
)

// socketPair returns the two ends of a loopback TCP connection: the end the
// gateway accepted, and the end the connector dialled.
//
// Loopback TCP, not net.Pipe. net.Pipe is synchronous and
// unbuffered, so a write blocks until the far side reads it, which hides the
// behaviour under test here, where fifty streams are interleaved on one
// connection and one slow reader must not stall the others.
func socketPair(t *testing.T) (gatewaySide, connectorSide net.Conn) {
	t.Helper()

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer listener.Close()

	type accepted struct {
		conn net.Conn
		err  error
	}
	inbound := make(chan accepted, 1)
	go func() {
		conn, err := listener.Accept()
		inbound <- accepted{conn, err}
	}()

	dialed, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	result := <-inbound
	if result.err != nil {
		t.Fatalf("accept: %v", result.err)
	}
	t.Cleanup(func() {
		_ = dialed.Close()
		_ = result.conn.Close()
	})
	return result.conn, dialed
}

// newConnector starts a connector with the given handler.
func newConnector(t *testing.T, handler http.Handler, limits Limits) *Connector {
	t.Helper()
	connector, err := NewConnector(ConnectorConfig{Handler: handler, Limits: limits})
	if err != nil {
		t.Fatalf("new connector: %v", err)
	}
	t.Cleanup(func() { _ = connector.Close() })
	return connector
}

// newPool starts a pool.
func newPool(t *testing.T, cfg PoolConfig) *Pool {
	t.Helper()
	pool, err := NewPool(cfg)
	if err != nil {
		t.Fatalf("new pool: %v", err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	return pool
}

// link dials a connector into a pool and returns the gateway's view of it.
func link(t *testing.T, pool *Pool, clusterID string, connector *Connector) *Conn {
	t.Helper()

	gatewaySide, connectorSide := socketPair(t)
	served := make(chan struct{})
	go func() {
		defer close(served)
		_ = connector.Serve(connectorSide)
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, err := pool.Adopt(ctx, clusterID, gatewaySide)
	if err != nil {
		t.Fatalf("adopt: %v", err)
	}

	t.Cleanup(func() {
		_ = conn.Close()
		select {
		case <-served:
		case <-time.After(5 * time.Second):
			t.Error("connector Serve did not return after the tunnel closed")
		}
	})
	return conn
}

// linkKillable is link with the connector's own socket handed back, so a test can
// take it away without warning.
//
// The returned func closes that socket and nothing else: no GOAWAY, no drain, no
// chance for the connector to say which streams it had not processed. That is a
// killed process as the gateway sees it: the kernel reaped the fd and the FIN is
// all that is left of the far end.
//
// It is a FIN instead of an RST, and the difference matters under
// load: see silentPeer.vanish. An RST outside the peer's receive window is
// dropped by its TCP, so a reset connector can leave a gateway that is behind on
// its reads believing the tunnel is healthy. A FIN is retransmitted until it
// lands.
func linkKillable(t *testing.T, pool *Pool, clusterID string, connector *Connector) (*Conn, func()) {
	t.Helper()

	gatewaySide, connectorSide := socketPair(t)
	served := make(chan struct{})
	go func() {
		defer close(served)
		_ = connector.Serve(connectorSide)
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, err := pool.Adopt(ctx, clusterID, gatewaySide)
	if err != nil {
		t.Fatalf("adopt: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })

	return conn, func() { _ = connectorSide.Close() }
}

// gatewayShapedRequest builds a request the way the gateway's own handler leaves
// one: a body over a buffer it already read, ContentLength from that buffer, and
// no GetBody. See gateway.Handler, which does exactly this before handing the
// request to the proxy.
//
// It matters that this is not http.NewRequest with a strings.Reader. net/http
// fills GetBody in for *strings.Reader, *bytes.Reader and *bytes.Buffer, so a
// request built that way is already replayable before the pool sees it, which is
// why every test in this package used to pass with makeReplayable removed.
func gatewayShapedRequest(t *testing.T, path, body string) *http.Request {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, "https://ignored"+path, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Body = io.NopCloser(bytes.NewReader([]byte(body)))
	req.ContentLength = int64(len(body))
	if req.GetBody != nil {
		t.Fatal("a request built this way must not arrive with a GetBody, or it proves nothing")
	}
	return req
}

// occupy holds stream slots on a tunnel so the pool's least-loaded pick prefers
// a different one. Reservations are only released by a RoundTrip, so this is
// permanent for the life of the test, which is what makes the pick
// deterministic.
func occupy(conn *Conn, slots int) {
	for i := 0; i < slots; i++ {
		conn.cc.ReserveNewRequest()
	}
}

// proxyTo builds the connector-side handler: a reverse proxy to a local
// upstream, configured exactly as the gateway's own is.
func proxyTo(upstream string) *httputil.ReverseProxy {
	target, err := url.Parse(upstream)
	if err != nil {
		panic(err)
	}
	return &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(target)
			r.Out.Host = target.Host
		},
		FlushInterval: -1,
	}
}

// gatewayProxy builds the gateway-side reverse proxy over a tunnel, matching
// gateway.NewReverseProxy's configuration: flush every write so a stream arrives
// token by token, and read the tail of every response for its usage object.
func gatewayProxy(pool *Pool, clusterID string, modify func(*http.Response) error) *httputil.ReverseProxy {
	authority := Authority(clusterID)
	return &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(authority)
			r.Out.Host = authority.Host
		},
		FlushInterval:  -1,
		ModifyResponse: modify,
		Transport:      pool.RoundTripper(clusterID),
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			w.Header().Set("X-Tunnel-Fault", Classify(err).String())
			w.WriteHeader(http.StatusBadGateway)
		},
	}
}

// gates hands out a lock-step channel per request, so a streaming test can
// require that chunk N is written only after chunk N-1 was observed by the
// client. Anything that buffers deadlocks instead of passing slowly.
type gates struct {
	mu sync.Mutex
	m  map[string]chan struct{}
}

func newGates() *gates { return &gates{m: make(map[string]chan struct{})} }

func (g *gates) get(id string) chan struct{} {
	g.mu.Lock()
	defer g.mu.Unlock()
	gate, ok := g.m[id]
	if !ok {
		gate = make(chan struct{}, 1)
		g.m[id] = gate
	}
	return gate
}

// counter is a concurrency-safe tally by key.
type counter struct {
	mu sync.Mutex
	m  map[string]int
}

func newCounter() *counter { return &counter{m: make(map[string]int)} }

func (c *counter) add(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.m[key]++
}

func (c *counter) get(key string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.m[key]
}

func (c *counter) snapshot() map[string]int {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make(map[string]int, len(c.m))
	for k, v := range c.m {
		out[k] = v
	}
	return out
}

func (c *counter) total() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	total := 0
	for _, n := range c.m {
		total += n
	}
	return total
}

// eventuallyCount is eventually for a number, so a failure says what the number
// was instead of only that it was wrong.
func eventuallyCount(t *testing.T, within time.Duration, what string, want int, count func() int) {
	t.Helper()
	deadline := time.Now().Add(within)
	got := count()
	for time.Now().Before(deadline) {
		if got == want {
			return
		}
		time.Sleep(2 * time.Millisecond)
		got = count()
	}
	t.Fatalf("timed out after %s waiting for %s: it is %d, want %d", within, what, got, want)
}

// eventually polls until cond holds or the deadline passes.
func eventually(t *testing.T, within time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timed out after %s waiting for %s", within, what)
}

// silentPeer is a hand-rolled HTTP/2 server end that does the handshake, answers
// one PING, and then does exactly what it is told and nothing else.
//
// It exists for the three cases a real connector cannot produce on demand: a
// GOAWAY whose LastStreamID is below every stream the gateway opened, the
// "received but never processed" case that must be retried, a peer that stops
// answering without closing the socket, and a peer whose socket is torn down
// with an RST and no readable GOAWAY at all.
type silentPeer struct {
	conn    net.Conn
	framer  *http2.Framer
	silence chan struct{}
	drain   chan struct{}
	abrupt  chan struct{}
	drained chan struct{}
	died    chan struct{}
	done    chan struct{}
	// headers carries the stream id of every HEADERS frame the peer reads, which
	// is the only way to know a request was really written, not merely
	// given a stream slot.
	headers chan uint32
	// rst chooses between the two abrupt deaths. Written before the channel that
	// triggers the close, so the peer goroutine reads it in order.
	rst    bool
	limits Limits
}

func newSilentPeer(t *testing.T, conn net.Conn, maxStreams uint32) *silentPeer {
	t.Helper()
	peer := &silentPeer{
		conn:    conn,
		framer:  http2.NewFramer(conn, conn),
		silence: make(chan struct{}),
		drain:   make(chan struct{}),
		abrupt:  make(chan struct{}),
		drained: make(chan struct{}),
		died:    make(chan struct{}),
		done:    make(chan struct{}),
		headers: make(chan uint32, 256),
	}
	peer.limits = DefaultLimits()
	if maxStreams > 0 {
		peer.limits.MaxConcurrentStreams = maxStreams
	}
	go peer.run(t)
	return peer
}

// run does the handshake and then answers frames until told to go silent or to
// send GOAWAY.
//
// The handshake is in here, not in the constructor because the gateway
// writes the client preface from Adopt, which has not been called yet when the
// peer is built.
//
// Reads happen on their own goroutine and every write happens on this one, which
// is how x/net's own server uses a Framer.
func (p *silentPeer) run(t *testing.T) {
	defer close(p.done)

	preface := make([]byte, len(http2.ClientPreface))
	if _, err := io.ReadFull(p.conn, preface); err != nil {
		return
	}
	if string(preface) != http2.ClientPreface {
		t.Errorf("bad client preface %q", preface)
		return
	}
	settings := []http2.Setting{
		{ID: http2.SettingMaxConcurrentStreams, Val: p.limits.MaxConcurrentStreams},
		{ID: http2.SettingInitialWindowSize, Val: uint32(p.limits.StreamWindow)},
		{ID: http2.SettingMaxFrameSize, Val: p.limits.MaxReadFrameSize},
	}
	if err := p.framer.WriteSettings(settings...); err != nil {
		return
	}

	frames := make(chan http2.Frame, 64)
	go func() {
		defer close(frames)
		for {
			frame, err := p.framer.ReadFrame()
			if err != nil {
				return
			}
			select {
			case frames <- frame:
			case <-p.done:
				return
			}
		}
	}()

	silence := p.silence
	drain := p.drain
	silent := false
	for {
		select {
		case <-silence:
			silence = nil
			silent = true
		case <-drain:
			drain = nil
			_ = p.framer.WriteGoAway(0, http2.ErrCodeNo, nil)
			close(p.drained)
		case <-p.abrupt:
			p.reset()
			close(p.died)
			return
		case frame, ok := <-frames:
			if !ok {
				return
			}
			if headers, ok := frame.(*http2.HeadersFrame); ok {
				select {
				case p.headers <- headers.StreamID:
				default:
				}
			}
			if silent {
				continue
			}
			switch f := frame.(type) {
			case *http2.SettingsFrame:
				if !f.IsAck() {
					_ = p.framer.WriteSettingsAck()
				}
			case *http2.PingFrame:
				if !f.IsAck() {
					_ = p.framer.WritePing(true, f.Data)
				}
			}
		}
	}
}

// awaitRequests blocks until the peer has read the headers of n distinct streams.
//
// Waiting on Conn.Streams instead is not the same claim and is the flake it
// replaced: a stream slot is reserved before the request is written, and a
// request the gateway has not written yet is one x/net may retry without a
// GetBody: errClientConnUnusable is the one error shouldRetryRequest replays
// bare. Every test that kills a tunnel under a request has to know the request is
// really on the wire, or it silently tests the easy case instead.
func (p *silentPeer) awaitRequests(t *testing.T, n int) {
	t.Helper()
	seen := make(map[uint32]bool, n)
	for len(seen) < n {
		select {
		case id := <-p.headers:
			seen[id] = true
		case <-time.After(10 * time.Second):
			t.Fatalf("only %d of %d requests reached the peer", len(seen), n)
		}
	}
}

// goSilent stops the peer answering anything, without closing the socket. This
// is a node that vanished: no FIN, no RST, just nothing.
func (p *silentPeer) goSilent() { close(p.silence) }

// drainGracefully is a connector that was asked to stop: GOAWAY announcing that
// it processed nothing, and then the socket held open while the streams it did
// accept finish. It returns once the frame is on the wire.
//
// Holding the socket is the point of the distinction. Closing a loopback
// socket that still has unread inbound data on it sends an RST, and an RST
// discards the GOAWAY the gateway has not read yet, which turns this case into
// dieAbruptly by accident, and is exactly the flake this pair of methods
// replaced.
func (p *silentPeer) drainGracefully(t *testing.T) {
	t.Helper()
	close(p.drain)
	select {
	case <-p.drained:
	case <-time.After(5 * time.Second):
		t.Fatal("the peer never wrote its GOAWAY")
	}
}

// dieAbruptly is a connector that was SIGKILLed, OOMKilled, or whose node went
// away, in the flavour where the socket is RESET: no GOAWAY, no FIN, an RST.
//
// SetLinger(0) is what makes close send an RST. Use it where the gateway is not
// behind on its reads; see vanish for why that qualification matters.
func (p *silentPeer) dieAbruptly(t *testing.T) {
	t.Helper()
	p.rst = true
	close(p.abrupt)
	select {
	case <-p.died:
	case <-time.After(5 * time.Second):
		t.Fatal("the peer never reset its socket")
	}
}

// vanish is the same death in the flavour where the socket is CLOSED: no GOAWAY,
// a FIN, which is what a killed process's fds produce when nothing is left unread
// on them.
//
// It is the flavour to reach for under load. An RST is only accepted by the peer's
// TCP if its sequence number falls inside the receive window, so a connector reset
// while the gateway is behind on its reads can have its RST dropped and leave the
// gateway holding a socket that is, as far as it knows, fine. A FIN is part of the
// byte stream and is retransmitted until acknowledged, so it cannot be lost. Both
// happen in a real cluster and the gateway has to survive both; only one of them
// makes a deterministic test.
func (p *silentPeer) vanish(t *testing.T) {
	t.Helper()
	close(p.abrupt)
	select {
	case <-p.died:
	case <-time.After(5 * time.Second):
		t.Fatal("the peer never closed its socket")
	}
}

func (p *silentPeer) reset() {
	if tcp, ok := p.conn.(*net.TCPConn); ok && p.rst {
		_ = tcp.SetLinger(0)
	}
	_ = p.conn.Close()
}
