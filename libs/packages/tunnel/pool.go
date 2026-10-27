package tunnel

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/net/http2"
)

// ErrNoTunnel means no agent for the cluster is currently connected. It is
// capacity, not a failure of the cluster: nothing was attempted.
var ErrNoTunnel = errors.New("tunnel: no live tunnel to cluster")

// ErrSaturated means every tunnel to the cluster is at its stream limit.
//
// It exists because the alternative is silence. x/net blocks a request in
// awaitOpenSlotForStreamLocked until a slot frees, so without this the shape of
// overload is a latency graph with no errors on it — and a caller waiting an
// unbounded time for a stream slot would rather be told there is no capacity.
var ErrSaturated = errors.New("tunnel: all tunnels to cluster are at their stream limit")

// ErrNoCluster means a request reached the pool without a cluster on it, which
// is a programming error in whatever built the request.
var ErrNoCluster = errors.New("tunnel: request carries no cluster")

// ErrBodyTooLarge means a request body is past what the pool will hold in
// memory to keep the request replayable.
//
// It refuses rather than truncating. A silently shortened body is a request the
// cluster answers wrongly, which is worse than a request that fails, and the
// caller's own cap means it should never be reachable from the gateway.
var ErrBodyTooLarge = errors.New("tunnel: request body too large to keep replayable")

// ErrBodyIdle means a response body went unread for longer than the pool's
// BodyIdleTimeout and was closed to give its flow-control window back to the
// tunnel. The caller had stopped reading, so this is the caller's ending, not
// the cluster's.
var ErrBodyIdle = errors.New("tunnel: response body unread past the idle timeout")

// DefaultBodyIdleTimeout is how long a response body may sit unread before the
// pool closes it.
//
// Unread response bytes hold the connection's flow-control window, and the
// window is shared by every stream on the tunnel: a caller that stops reading,
// and a gateway write to it that blocks, would otherwise freeze generation for
// every other request to the cluster on that tunnel. A Read waiting for the
// cluster's next token does not count as idle — only time between Reads does.
const DefaultBodyIdleTimeout = 2 * time.Minute

// ErrPoolClosed means the pool has been shut down or closed and adopts nothing
// more.
var ErrPoolClosed = errors.New("tunnel: pool closed")

// ErrTunnelLimit means adopting a tunnel would take a cluster, or the pool, past
// its cap.
//
// One enrolment token may dial as often as it likes, and every tunnel costs two
// goroutines and up to ConnWindow of receive buffer here. Without a cap a leaked
// token, or an agent stuck in a redial loop, grows this process without bound.
var ErrTunnelLimit = errors.New("tunnel: tunnel limit reached")

// DefaultMaxTunnelsPerCluster is how many tunnels one cluster may hold on one
// gateway replica. An agent holds a couple per replica and a cluster runs a
// handful of agents, so this is far above any honest install.
const DefaultMaxTunnelsPerCluster = 64

// DefaultMaxTunnels is how many tunnels one gateway replica holds in total.
const DefaultMaxTunnels = 4096

// maxReplayBytes caps the body the pool will hold in order to keep a request
// replayable. Matched to the gateway's own request cap — move them together.
//
// This guards the path that arrives without a buffer already read. The gateway
// does not take it: it has the body from routing and passes it to `Replayable`,
// which reuses that buffer and is bounded by `maxRequestBodyBytes` instead.
const maxReplayBytes = 100 << 20

// maxTunnelAttempts bounds how many tunnels one request may be offered to.
//
// HTTP/2 retries the endings it recognises on its own; this budget exists for
// the ones it does not, which is a tunnel that died without a readable GOAWAY.
// It is small because a rollout replaces one agent at a time, so a request
// meets at most one dying tunnel per attempt, and a request that has met three
// is not being unlucky.
const maxTunnelAttempts = 3

// authoritySuffix is the synthetic name a tunnelled request is addressed to.
//
// http2's RoundTrip rejects any scheme but http and https and needs an
// authority to build :authority from, and a tunnel has no address — the
// connection is the address. So the URL is a placeholder and the cluster
// travels on the request context, where nothing can confuse it with a
// resolvable name.
const authoritySuffix = ".tunnel.hangar.internal"

// Observer is told when a cluster's tunnel coverage changes.
//
// This is where reachability is measured, and it is the only place it is. The
// gateway does not write observed state: that has exactly one writer, and it is
// ingestion. Coverage is a gauge on this process, like routing.Observer's.
type Observer interface {
	TunnelOpened(clusterID string, live int)
	TunnelClosed(clusterID string, live int)
}

// RetryObserver is optionally implemented by an Observer to count what became
// of requests whose tunnel died under them with no GOAWAY. The outcome is one
// of the Retry constants.
type RetryObserver interface {
	TunnelRetry(clusterID, outcome string)
}

// What became of a request whose tunnel died. Closed set: these reach a metric
// label.
const (
	// RetryResent is a request sent down another tunnel.
	RetryResent = "resent"
	// RetryWritten is a request not resent because its headers had been
	// written and it is not idempotent, so the agent may already have
	// forwarded it.
	RetryWritten = "written"
	// RetryExhausted is a request that could have been resent but had used its
	// attempts, or whose body could not be produced again.
	RetryExhausted = "exhausted"
)

// PoolConfig configures the gateway end.
type PoolConfig struct {
	// Limits are the HTTP/2 settings this end applies. Zero fields default.
	Limits Limits
	// ResponseHeaderTimeout bounds how long a cluster may hold a stream open
	// without answering. Zero means DefaultResponseHeaderTimeout.
	//
	// It is not a round-trip timeout and must not become one. Generation at a
	// long context legitimately runs for minutes, and the timer is disarmed the
	// moment the first response header arrives.
	ResponseHeaderTimeout time.Duration
	// HealthInterval is how often a tunnel is checked for having quietly died.
	// Zero means DefaultHealthInterval.
	HealthInterval time.Duration
	// Observer, when set, is told about coverage changes.
	Observer Observer
	// MaxTunnelsPerCluster caps the tunnels one cluster holds on this replica.
	// Zero means DefaultMaxTunnelsPerCluster.
	MaxTunnelsPerCluster int
	// MaxTunnels caps the tunnels this replica holds across every cluster. Zero
	// means DefaultMaxTunnels.
	MaxTunnels int
	// BodyIdleTimeout closes a response body nobody has read from for this long.
	// Zero means DefaultBodyIdleTimeout; negative turns it off.
	BodyIdleTimeout time.Duration
}

// DefaultResponseHeaderTimeout is how long a cluster gets to produce a response
// header. Prefill on a long prompt is slow; a serving runtime that has accepted
// a stream and said nothing for this long is wedged, not thinking.
//
// Sized for the non-streamed case, which is the one that pays. A streaming
// completion sends its header at the first token and disarms this timer in
// seconds, so it never sees the number at all; a non-streamed one sends no
// header until the last token is generated, so for that shape this timer covers
// the entire generation, prefill included.
//
// Nothing in the path caps max_tokens, and a long-context generation of 140k
// output tokens at ~90 tok/s is about 25 minutes. Non-streamed, that whole
// generation sits inside this timer, with prefill in front of it, so forty-five
// minutes leaves room.
//
// It is still a bound and not the absence of one. A request waiting here holds
// one of the agent's concurrent stream slots for the whole wait, so a frontend
// that accepted a stream and has said nothing for three quarters of an hour is
// wedged, not thinking — and the number is small enough that one on-call shift
// catches it.
const DefaultResponseHeaderTimeout = 45 * time.Minute

// InnerHopMargin is how much longer each hop towards the serving runtime waits
// than the hop outside it.
const InnerHopMargin = time.Minute

// InnerHopTimeout is the response-header bound for a hop closer to the serving
// runtime than the one holding budget.
//
// The inference path arms this timer three times — the gateway's upstream
// transport, the tunnel, and the agent's proxy to the local frontend — and only
// the smallest of the three is ever observed. That is the whole failure this
// function exists to prevent: raising one hop and not the others changes nothing
// a caller can see, because the innermost still fires first and the gateway
// still answers upstream_unavailable at the old wall. So every hop derives from
// one number, and the inner hops add this margin rather than matching exactly,
// so that when something really is wedged it is the outermost hop — the only one
// holding the caller's connection — that gives up first and says so.
func InnerHopTimeout(budget time.Duration) time.Duration {
	if budget <= 0 {
		budget = DefaultResponseHeaderTimeout
	}
	return budget + InnerHopMargin
}

// DefaultHealthInterval is how often a tunnel that x/net has closed is noticed.
const DefaultHealthInterval = time.Second

// Pool holds every tunnel the gateway currently has, keyed by cluster.
//
// It is an http2.ClientConnPool, which is the trick that keeps HTTP/2's own
// retry: http2.Transport.RoundTrip asks the pool for a connection, and asks
// again after a failure that shouldRetryRequest says is replayable. So a
// request the dying agent never processed is retried down a sibling tunnel
// without this package reimplementing — or mis-reimplementing — the sentinel
// checks that decide which failures those are.
type Pool struct {
	// t2 keeps the net/http Transport it was configured from alive internally,
	// which is what makes ResponseHeaderTimeout and the flow-control windows
	// reachable from a ClientConn. There is nothing else to hold.
	t2  *http2.Transport
	cfg PoolConfig

	mu       sync.Mutex
	clusters map[string][]*Conn
	byConn   map[*http2.ClientConn]*Conn
	cursor   map[string]uint64
	closed   bool

	// reportMu orders coverage reports to the Observer. See report.
	reportMu sync.Mutex
}

type clusterKey struct{}

// NewPool builds the gateway end.
//
// It goes through http2.ConfigureTransports rather than constructing an
// http2.Transport, for two things that are only reachable that way:
// ClientConn.responseHeaderTimeout reads net/http's Transport field, and the
// flow-control windows are read off net/http's HTTP2Config. Hand-building an
// http2.Transport silently gives up both — the timeout becomes zero, meaning no
// timeout at all, and the windows become whatever the x/net in go.mod defaults
// to this month.
func NewPool(cfg PoolConfig) (*Pool, error) {
	limits := cfg.Limits.withDefaults()
	if cfg.ResponseHeaderTimeout == 0 {
		cfg.ResponseHeaderTimeout = DefaultResponseHeaderTimeout
	}
	if cfg.HealthInterval == 0 {
		cfg.HealthInterval = DefaultHealthInterval
	}
	if cfg.MaxTunnelsPerCluster <= 0 {
		cfg.MaxTunnelsPerCluster = DefaultMaxTunnelsPerCluster
	}
	if cfg.MaxTunnels <= 0 {
		cfg.MaxTunnels = DefaultMaxTunnels
	}
	if cfg.BodyIdleTimeout == 0 {
		cfg.BodyIdleTimeout = DefaultBodyIdleTimeout
	}

	// MaxConcurrentStreams is deliberately absent. It is a server setting: x/net's
	// client advertises ENABLE_PUSH, INITIAL_WINDOW_SIZE, MAX_FRAME_SIZE and
	// MAX_HEADER_LIST_SIZE and nothing else, and configFromTransport never reads
	// the value, so setting it here would look like a per-tunnel stream cap and be
	// worth nothing. The cap that exists is the agent's, learned from its
	// SETTINGS and enforced by GetClientConn.
	t1 := &http.Transport{
		ResponseHeaderTimeout: cfg.ResponseHeaderTimeout,
		HTTP2: &http.HTTP2Config{
			MaxReadFrameSize:              int(limits.MaxReadFrameSize),
			MaxReceiveBufferPerConnection: int(limits.ConnWindow),
			MaxReceiveBufferPerStream:     int(limits.StreamWindow),
			SendPingTimeout:               limits.ReadIdleTimeout,
			PingTimeout:                   limits.PingTimeout,
			WriteByteTimeout:              limits.WriteByteTimeout,
		},
	}
	t2, err := http2.ConfigureTransports(t1)
	if err != nil {
		return nil, err
	}

	pool := &Pool{
		t2:       t2,
		cfg:      cfg,
		clusters: make(map[string][]*Conn),
		byConn:   make(map[*http2.ClientConn]*Conn),
		cursor:   make(map[string]uint64),
	}
	// Set before the transport is used for anything: http2.Transport resolves
	// its pool once, on first use, and keeps whatever it found.
	t2.ConnPool = pool
	return pool, nil
}

// Authority is the placeholder URL a tunnelled request is addressed to.
//
// A caller building a reverse proxy passes this where it would otherwise pass a
// cluster's public serving address. It never resolves and is never dialled.
func Authority(clusterID string) *url.URL {
	return &url.URL{Scheme: "https", Host: dnsLabel(clusterID) + authoritySuffix}
}

// dnsLabel makes a cluster id safe to put in an authority. It is cosmetic: the
// pool routes on the request context, not on this string.
func dnsLabel(id string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(id) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '-':
			b.WriteRune(r)
		default:
			b.WriteByte('-')
		}
	}
	if b.Len() == 0 {
		return "unknown"
	}
	return b.String()
}

// Adopt takes a connection an agent dialled in and starts serving requests
// to clusterID over it.
//
// The tunnel is verified before it is offered to traffic: one PING, answered.
// That proves the far end is really speaking HTTP/2 and — because a conforming
// peer sends SETTINGS before anything else — that its advertised stream limit
// has arrived. Admitting requests before then would admit them against x/net's
// assumed 100 streams rather than the agent's real limit.
//
// The pool's state is checked twice: before the handshake, so a dial that
// cannot be kept costs nothing, and again under the lock that adds it, because
// a Shutdown or a sibling dial can land while the PING is in flight. A tunnel
// adopted after Shutdown had collected the tunnels to drain would never be
// drained.
func (p *Pool) Adopt(ctx context.Context, clusterID string, raw net.Conn) (*Conn, error) {
	if clusterID == "" {
		_ = raw.Close()
		return nil, ErrNoCluster
	}
	if err := p.Admit(clusterID); err != nil {
		_ = raw.Close()
		return nil, err
	}

	cc, err := p.t2.NewClientConn(raw)
	if err != nil {
		_ = raw.Close()
		return nil, fmt.Errorf("tunnel: handshake with %s: %w", clusterID, err)
	}
	if err := cc.Ping(ctx); err != nil {
		_ = cc.Close()
		return nil, fmt.Errorf("tunnel: verifying %s: %w", clusterID, err)
	}

	conn := &Conn{
		ClusterID:  clusterID,
		RemoteAddr: addrString(raw),
		Opened:     time.Now(),
		cc:         cc,
		done:       make(chan struct{}),
	}

	p.mu.Lock()
	if err := p.admitLocked(clusterID); err != nil {
		p.mu.Unlock()
		_ = cc.Close()
		return nil, err
	}
	p.clusters[clusterID] = append(p.clusters[clusterID], conn)
	p.byConn[cc] = conn
	p.mu.Unlock()

	go conn.watch(p.cfg.HealthInterval)
	go func() {
		<-conn.Done()
		p.forget(conn)
	}()
	p.report(clusterID, true)
	return conn, nil
}

// Admit reports whether the pool would adopt one more tunnel to clusterID right
// now. The listener asks before it accepts a dial, so an agent past the cap is
// told why rather than handed a connection that is closed under it. Adopt asks
// again, so the answer is advice and not a reservation.
func (p *Pool) Admit(clusterID string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.admitLocked(clusterID)
}

func (p *Pool) admitLocked(clusterID string) error {
	if p.closed {
		return ErrPoolClosed
	}
	if held := len(p.clusters[clusterID]); held >= p.cfg.MaxTunnelsPerCluster {
		return fmt.Errorf("%w: cluster %s already holds %d tunnels on this replica", ErrTunnelLimit, clusterID, held)
	}
	if held := len(p.byConn); held >= p.cfg.MaxTunnels {
		return fmt.Errorf("%w: this replica already holds %d tunnels", ErrTunnelLimit, held)
	}
	return nil
}

func addrString(conn net.Conn) string {
	if addr := conn.RemoteAddr(); addr != nil {
		return addr.String()
	}
	return "unknown"
}

// forget drops a retired tunnel from the pool.
func (p *Pool) forget(conn *Conn) {
	p.mu.Lock()
	remaining := p.clusters[conn.ClusterID]
	kept := remaining[:0]
	found := false
	for _, candidate := range remaining {
		if candidate == conn {
			found = true
			continue
		}
		kept = append(kept, candidate)
	}
	if len(kept) == 0 {
		delete(p.clusters, conn.ClusterID)
		delete(p.cursor, conn.ClusterID)
	} else {
		p.clusters[conn.ClusterID] = kept
	}
	delete(p.byConn, conn.cc)
	p.mu.Unlock()

	if found {
		p.report(conn.ClusterID, false)
	}
}

// report tells the Observer that a cluster's coverage changed.
//
// The count is taken inside reportMu, after the change, and not by the
// caller. Two adopts that each counted on their own and then reported could
// reach the Observer in the opposite order, leaving a gauge that is Set to 1
// while the pool holds 2 until the next change. Serialised like this, the last
// report always counts every change before it.
//
// Not under p.mu: counting asks each tunnel's ClientConn for its state, which
// can wait on a stalled socket write (see snapshotLocked).
func (p *Pool) report(clusterID string, opened bool) {
	if p.cfg.Observer == nil {
		return
	}
	p.reportMu.Lock()
	defer p.reportMu.Unlock()
	live := p.Live(clusterID)
	if opened {
		p.cfg.Observer.TunnelOpened(clusterID, live)
	} else {
		p.cfg.Observer.TunnelClosed(clusterID, live)
	}
}

// snapshotLocked copies a cluster's tunnels so they can be inspected without
// p.mu. Conn.Live and Conn.Streams go through ClientConn.State, which waits on
// the connection's write lock, and that lock is held across a socket write for
// up to WriteByteTimeout. Asking under p.mu would let one stalled tunnel block
// routing to every cluster. The caller holds p.mu.
func (p *Pool) snapshotLocked(clusterID string) []*Conn {
	conns := p.clusters[clusterID]
	if len(conns) == 0 {
		return nil
	}
	return append([]*Conn(nil), conns...)
}

func countLive(conns []*Conn) int {
	live := 0
	for _, conn := range conns {
		if conn.Live() {
			live++
		}
	}
	return live
}

// Live is how many usable tunnels a cluster has right now.
//
// A gateway replica holding zero tunnels for every cluster is a replica that
// can serve nothing, and it must not report itself ready. That is this
// function's job, and it is the reason a gateway replica has a stable identity:
// coverage has to be a fact, not a hope about where a load balancer sent the
// last dial.
func (p *Pool) Live(clusterID string) int {
	p.mu.Lock()
	conns := p.snapshotLocked(clusterID)
	p.mu.Unlock()
	return countLive(conns)
}

// Coverage is Live for every cluster that has at least one tunnel.
func (p *Pool) Coverage() map[string]int {
	p.mu.Lock()
	snapshot := make(map[string][]*Conn, len(p.clusters))
	for cluster := range p.clusters {
		snapshot[cluster] = p.snapshotLocked(cluster)
	}
	p.mu.Unlock()

	coverage := make(map[string]int, len(snapshot))
	for cluster, conns := range snapshot {
		if live := countLive(conns); live > 0 {
			coverage[cluster] = live
		}
	}
	return coverage
}

// Clusters lists which clusters are reachable, sorted.
func (p *Pool) Clusters() []string {
	coverage := p.Coverage()
	clusters := make([]string, 0, len(coverage))
	for cluster := range coverage {
		clusters = append(clusters, cluster)
	}
	sort.Strings(clusters)
	return clusters
}

// RoundTripper returns the transport for one cluster.
//
// It is an ordinary http.RoundTripper, which is the entire point: a caller
// hands it to httputil.ReverseProxy and every behaviour the proxy already
// relies on — FlushInterval, ModifyResponse, ErrorHandler — works unchanged.
func (p *Pool) RoundTripper(clusterID string) http.RoundTripper {
	return &clusterTransport{pool: p, clusterID: clusterID}
}

type clusterTransport struct {
	pool      *Pool
	clusterID string
}

// RoundTrip addresses a request at a cluster and hands it to HTTP/2.
//
// The request is cloned rather than edited: a RoundTripper may not modify what
// it was given, and the proxy above this one reuses the inbound request.
//
// The loop is not a second implementation of HTTP/2's retry, which still runs
// inside t2.RoundTrip and still owns every ending it recognises: a GOAWAY that
// names the stream as unprocessed, or REFUSED_STREAM. It covers a tunnel that
// died with no GOAWAY on it, and only while that cannot duplicate work: see
// retryAfterTunnelDeath.
func (t *clusterTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	authority := Authority(t.clusterID)
	ctx := context.WithValue(r.Context(), clusterKey{}, t.clusterID)
	ctx = context.WithValue(ctx, attemptKey{}, &attempts{})
	// Set once any attempt has started writing request headers. From then on the
	// agent may have the request, and may already have forwarded it.
	var wrote atomic.Bool
	ctx = httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{
		WroteHeaders: func() { wrote.Store(true) },
	})
	clone := r.Clone(ctx)
	clone.URL.Scheme = authority.Scheme
	clone.URL.Host = authority.Host
	if clone.Host == "" {
		clone.Host = authority.Host
	}
	if err := makeReplayable(clone); err != nil {
		return nil, err
	}

	var first error
	for attempt := 1; ; attempt++ {
		resp, err := t.pool.t2.RoundTrip(clone)
		if err == nil {
			watchIdle(resp, t.pool.cfg.BodyIdleTimeout)
			return resp, nil
		}
		if first == nil {
			first = err
		}
		if !retryAfterTunnelDeath(clone, err, wrote.Load()) {
			if clone.Context().Err() == nil && isTunnelDeath(err) {
				t.retried(RetryWritten)
			}
			return nil, worseOf(first, err)
		}
		if attempt >= maxTunnelAttempts {
			t.retried(RetryExhausted)
			return nil, worseOf(first, err)
		}
		rewound, ok := rewind(clone)
		if !ok {
			t.retried(RetryExhausted)
			return nil, worseOf(first, err)
		}
		t.retried(RetryResent)
		clone = rewound
	}
}

func (t *clusterTransport) retried(outcome string) {
	if observer, ok := t.pool.cfg.Observer.(RetryObserver); ok {
		observer.TunnelRetry(t.clusterID, outcome)
	}
}

// worseOf keeps whichever ending describes what actually happened.
//
// A request that met a dying tunnel and then found no live one has met both, and
// the second is the misleading one: ErrNoTunnel and ErrSaturated both say nothing
// was attempted, and something was. Reporting them would move a request that was
// written to a socket into the bucket for requests the gateway never sent.
func worseOf(first, last error) error {
	if first == nil || errors.Is(last, first) {
		return last
	}
	if errors.Is(last, ErrNoTunnel) || errors.Is(last, ErrSaturated) {
		return first
	}
	return last
}

// retryAfterTunnelDeath reports whether this request may be sent down another
// tunnel.
//
// It only ever runs on the error return of RoundTrip, so no byte of this answer
// has reached a caller. A request whose response headers arrived is not here —
// its body errors belong to whoever is reading the body, and replaying then
// would splice two generations into one answer.
//
// No response is not the same as not processed. Once the request headers have
// been written the agent may have read them and forwarded the request to the
// serving runtime, which may be generating — and billing — right now. A tunnel
// that dies without a GOAWAY says nothing about which of those happened, so a
// non-idempotent request is retried only when it provably never left: headers
// not yet written. Idempotent methods, and requests that carry an
// Idempotency-Key, are retried as net/http retries them. Everything else fails
// as churn and the caller decides.
func retryAfterTunnelDeath(r *http.Request, err error, wroteHeaders bool) bool {
	if r.Context().Err() != nil {
		return false
	}
	if !isTunnelDeath(err) {
		return false
	}
	return !wroteHeaders || idempotent(r)
}

// idempotent mirrors net/http's rule for replaying a request whose fate is
// unknown: a method that is safe to repeat, or an idempotency key the caller set.
func idempotent(r *http.Request) bool {
	switch r.Method {
	case "", http.MethodGet, http.MethodHead, http.MethodOptions, http.MethodTrace:
		return true
	}
	if _, ok := r.Header["Idempotency-Key"]; ok {
		return true
	}
	if _, ok := r.Header["X-Idempotency-Key"]; ok {
		return true
	}
	return false
}

// rewind returns an equivalent request with its body back at the start, or false
// if the body cannot be produced again.
func rewind(r *http.Request) (*http.Request, bool) {
	if r.Body == nil || r.Body == http.NoBody {
		return r, true
	}
	if r.GetBody == nil {
		return nil, false
	}
	body, err := r.GetBody()
	if err != nil {
		return nil, false
	}
	next := r.Clone(r.Context())
	next.Body = body
	return next, true
}

// makeReplayable gives a request a GetBody so HTTP/2 may replay it.
//
// Without one, shouldRetryRequest refuses to retry anything with a body after a
// GOAWAY — which is every completion request there is. The gateway has already
// buffered the body to read its `model`, so the bytes are in memory either way;
// a caller that still holds that buffer should use Replayable and skip the
// second copy.
func makeReplayable(r *http.Request) error {
	return replayableWithin(r, maxReplayBytes)
}

// replayableWithin is makeReplayable with the cap as an argument, so the boundary
// is reachable without allocating the real one.
//
// A body past the cap is refused. Truncating it instead would send the cluster a
// prompt that is not the one the caller wrote and get back a plausible answer to
// it, which is the worst available outcome — and ContentLength is checked before
// the read so an oversized declared length costs nothing.
func replayableWithin(r *http.Request, limit int64) error {
	if r.Body == nil || r.Body == http.NoBody || r.GetBody != nil {
		return nil
	}
	if r.ContentLength > limit {
		return fmt.Errorf("%w: %d bytes declared, limit is %d", ErrBodyTooLarge, r.ContentLength, limit)
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, limit+1))
	_ = r.Body.Close()
	if err != nil {
		return fmt.Errorf("tunnel: buffering request body: %w", err)
	}
	if int64(len(body)) > limit {
		return fmt.Errorf("%w: limit is %d", ErrBodyTooLarge, limit)
	}
	setReplayBody(r, body)
	return nil
}

// Replayable marks a request as safe for HTTP/2 to send again, using a body the
// caller already holds.
//
// A request without this is not retried after an agent sends GOAWAY, so an
// agent rollout becomes 502s for exactly the requests that were never
// processed — the ones it is cheapest to get right.
func Replayable(r *http.Request, body []byte) *http.Request {
	setReplayBody(r, body)
	return r
}

func setReplayBody(r *http.Request, body []byte) {
	r.ContentLength = int64(len(body))
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(body)), nil
	}
}

// GetClientConn picks a tunnel for one request, or says why there is none.
//
// The reservation is what makes saturation fast. Asking whether a connection
// can take a request and then sending it is a race that http2 resolves by
// blocking; ReserveNewRequest holds the slot, and the RoundTrip that follows
// releases it. Every path out of here either reserves and returns a connection
// or returns an error, so a slot is never held by a request that was not sent.
func (p *Pool) GetClientConn(r *http.Request, _ string) (*http2.ClientConn, error) {
	clusterID, ok := r.Context().Value(clusterKey{}).(string)
	if !ok || clusterID == "" {
		return nil, ErrNoCluster
	}

	// The lock covers the copy and the cursor and nothing else; see
	// snapshotLocked for why the tunnels are not inspected under it.
	p.mu.Lock()
	conns := p.snapshotLocked(clusterID)
	offset := p.cursor[clusterID]
	p.cursor[clusterID]++
	p.mu.Unlock()

	if len(conns) == 0 {
		return nil, fmt.Errorf("%w %s", ErrNoTunnel, clusterID)
	}

	// Least-loaded, breaking ties by a rotating cursor. Least-loaded rather than
	// round-robin because streams on a tunnel are long: a completion holds its
	// stream for the length of the generation, so "how many are on there now"
	// is the only load signal that means anything.
	//
	// A tunnel this request has already been sent down loses to any tunnel it has
	// not, whatever the stream counts say. That is not a preference, it is the
	// retry working at all: the moment an agent is killed, every stream on its
	// tunnel fails, so for a few milliseconds the corpse has the fewest streams of
	// any tunnel to the cluster and least-loaded sends the retry straight back to
	// it. A tunnel already tried is only picked again when it is the only one left.
	//
	// Exactly one reservation is ever taken, because there is no exported way to
	// give one back — a speculative reserve on a candidate that then loses the
	// comparison would burn a stream slot until the tunnel closed.
	history := attemptsOf(r)
	tried := history.tunnels()
	var refused []*Conn
	for {
		live := 0
		var best, untried *Conn
		bestStreams, untriedStreams := -1, -1
		for i := range conns {
			conn := conns[(offset+uint64(i))%uint64(len(conns))]
			if !conn.Live() {
				continue
			}
			live++
			if contains(refused, conn) {
				continue
			}
			streams := conn.Streams()
			if bestStreams < 0 || streams < bestStreams {
				best, bestStreams = conn, streams
			}
			if !contains(tried, conn) && (untriedStreams < 0 || streams < untriedStreams) {
				untried, untriedStreams = conn, streams
			}
		}
		if live == 0 {
			return nil, fmt.Errorf("%w %s", ErrNoTunnel, clusterID)
		}
		if untried != nil {
			best = untried
		}
		if best == nil {
			return nil, fmt.Errorf("%w %s", ErrSaturated, clusterID)
		}
		if best.cc.ReserveNewRequest() {
			history.add(best)
			return best.cc, nil
		}
		refused = append(refused, best)
	}
}

type attemptKey struct{}

// attempts records which tunnels one request has already been sent down.
//
// It lives on the request context because the choice is GetClientConn's and
// nothing hands it back: x/net asks the pool for a connection and returns only
// what happened. Both retries read it — HTTP/2's own and the pool's.
type attempts struct {
	mu    sync.Mutex
	tried []*Conn
}

func attemptsOf(r *http.Request) *attempts {
	history, _ := r.Context().Value(attemptKey{}).(*attempts)
	if history == nil {
		return &attempts{}
	}
	return history
}

func (a *attempts) add(conn *Conn) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.tried = append(a.tried, conn)
}

func (a *attempts) tunnels() []*Conn {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.tried
}

func contains(conns []*Conn, want *Conn) bool {
	for _, conn := range conns {
		if conn == want {
			return true
		}
	}
	return false
}

// MarkDead is http2 telling the pool a connection is finished.
func (p *Pool) MarkDead(cc *http2.ClientConn) {
	p.mu.Lock()
	conn := p.byConn[cc]
	p.mu.Unlock()
	if conn != nil {
		conn.retire()
	}
}

// Evict severs every tunnel to one cluster and returns how many there were. It
// is for a credential that must stop working now; Listener.Revoke calls it.
func (p *Pool) Evict(clusterID string) int {
	p.mu.Lock()
	conns := p.snapshotLocked(clusterID)
	p.mu.Unlock()
	for _, conn := range conns {
		_ = conn.Close()
	}
	return len(conns)
}

// Shutdown drains every tunnel: GOAWAY out, then wait for the streams already
// on them.
//
// The gateway's inbound HTTP server is drained by its own Shutdown. Tunnels are
// on a different listener and nothing else reaches them, so without this a
// rollout of the gateway severs in-flight generations at the far end of a
// connection the caller cannot see.
func (p *Pool) Shutdown(ctx context.Context) error {
	p.mu.Lock()
	p.closed = true
	var conns []*Conn
	for _, cluster := range p.clusters {
		conns = append(conns, cluster...)
	}
	p.mu.Unlock()

	var wg sync.WaitGroup
	for _, conn := range conns {
		wg.Add(1)
		go func(conn *Conn) {
			defer wg.Done()
			_ = conn.Shutdown(ctx)
		}(conn)
	}
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Close severs every tunnel.
func (p *Pool) Close() error {
	p.mu.Lock()
	p.closed = true
	var conns []*Conn
	for _, cluster := range p.clusters {
		conns = append(conns, cluster...)
	}
	p.mu.Unlock()
	for _, conn := range conns {
		_ = conn.Close()
	}
	return nil
}

// watchIdle closes a response body that goes unread for longer than timeout.
//
// A 101 is left alone: its body is the upgraded connection, and a proxy reaches
// for its Write method.
func watchIdle(resp *http.Response, timeout time.Duration) {
	if timeout <= 0 || resp.Body == nil || resp.Body == http.NoBody || resp.StatusCode == http.StatusSwitchingProtocols {
		return
	}
	body := &idleBody{body: resp.Body, timeout: timeout}
	body.timer = time.AfterFunc(timeout, body.expire)
	resp.Body = body
}

// idleBody is a response body with an idle timer that runs only between Reads.
type idleBody struct {
	body    io.ReadCloser
	timeout time.Duration
	timer   *time.Timer

	mu      sync.Mutex
	reading bool
	expired bool
	closed  bool
}

func (b *idleBody) Read(p []byte) (int, error) {
	b.mu.Lock()
	if b.expired {
		b.mu.Unlock()
		return 0, ErrBodyIdle
	}
	b.reading = true
	b.timer.Stop()
	b.mu.Unlock()

	n, err := b.body.Read(p)

	b.mu.Lock()
	b.reading = false
	expired := b.expired
	if err == nil && !b.closed && !expired {
		b.timer.Reset(b.timeout)
	}
	b.mu.Unlock()
	if expired && err != nil {
		return n, ErrBodyIdle
	}
	return n, err
}

func (b *idleBody) Close() error {
	b.mu.Lock()
	b.closed = true
	b.timer.Stop()
	b.mu.Unlock()
	return b.body.Close()
}

// expire closes the body if nobody is reading it. Closing an HTTP/2 response
// body resets the stream and hands its unread bytes back to the connection's
// window, which is what frees the other streams on the tunnel.
func (b *idleBody) expire() {
	b.mu.Lock()
	if b.reading || b.closed {
		b.mu.Unlock()
		return
	}
	b.expired = true
	b.mu.Unlock()
	_ = b.body.Close()
}
