package tunnel

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"golang.org/x/net/http2"
)

// TestAWedgedUpstreamDoesNotLeakAStreamForever proves the response-header
// timeout survives role reversal.
//
// http2.Transport has no ResponseHeaderTimeout field, so the obvious way to
// build this (construct an http2.Transport and call NewClientConn) silently
// gives a serving runtime unlimited time to say nothing. The pool goes through
// http2.ConfigureTransports precisely so ClientConn.responseHeaderTimeout finds
// the net/http Transport's value.
func TestAWedgedUpstreamDoesNotLeakAStreamForever(t *testing.T) {
	t.Parallel()

	const headerTimeout = 300 * time.Millisecond
	wedged := make(chan struct{})
	t.Cleanup(func() { close(wedged) })

	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/wedged":
			// The stream is accepted and nothing is ever written to it. This is
			// what a serving runtime that has deadlocked on its KV cache looks
			// like from outside.
			select {
			case <-wedged:
			case <-r.Context().Done():
			}
		case "/slow":
			// Headers immediately, then a long silence, then bytes. Legitimate:
			// prefill on a long prompt takes as long as it takes.
			w.WriteHeader(http.StatusOK)
			flusher := http.NewResponseController(w)
			_ = flusher.Flush()
			time.Sleep(3 * headerTimeout)
			_, _ = w.Write([]byte("first token"))
			_ = flusher.Flush()
			time.Sleep(2 * headerTimeout)
			_, _ = w.Write([]byte(" and the rest"))
		}
	}), Limits{})

	pool := newPool(t, PoolConfig{ResponseHeaderTimeout: headerTimeout})
	link(t, pool, "cluster-ap", connector)
	transport := pool.RoundTripper("cluster-ap")

	t.Run("a wedged stream is cut off", func(t *testing.T) {
		req, err := http.NewRequest(http.MethodPost, "https://ignored/wedged", strings.NewReader(`{}`))
		if err != nil {
			t.Fatal(err)
		}
		start := time.Now()
		resp, err := transport.RoundTrip(req)
		elapsed := time.Since(start)
		if err == nil {
			_ = resp.Body.Close()
			t.Fatal("a stream that never answered completed")
		}
		if elapsed < headerTimeout {
			t.Errorf("gave up after %s, before the %s timeout", elapsed, headerTimeout)
		}
		if elapsed > 10*headerTimeout {
			t.Errorf("took %s to give up on a %s timeout", elapsed, headerTimeout)
		}
		if fault := Classify(err); fault != FaultHeaderTimeout {
			t.Errorf("classified as %s, want %s: %v", fault, FaultHeaderTimeout, err)
		}
		var netErr interface{ Timeout() bool }
		if !errors.As(err, &netErr) || !netErr.Timeout() {
			t.Errorf("error does not report itself as a timeout: %v", err)
		}
	})

	t.Run("a slow generation that is streaming is left alone", func(t *testing.T) {
		req, err := http.NewRequest(http.MethodPost, "https://ignored/slow", strings.NewReader(`{}`))
		if err != nil {
			t.Fatal(err)
		}
		resp, err := transport.RoundTrip(req)
		if err != nil {
			t.Fatalf("a generation that answered and then took its time was cut off: %v", err)
		}
		defer resp.Body.Close()
		body, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatalf("reading a slow body: %v", err)
		}
		if string(body) != "first token and the rest" {
			t.Errorf("body was %q", body)
		}
	})
}

// TestSaturationIsAFastFailureNotLatency covers the hazard that overload has no
// error signal at all.
//
// x/net's client blocks in awaitOpenSlotForStreamLocked when the peer's
// MaxConcurrentStreams is reached; it does not refuse. Left alone, a cluster at
// capacity produces a latency graph and no failures, and the gateway has no way
// to tell a caller there is no room.
func TestSaturationIsAFastFailureNotLatency(t *testing.T) {
	t.Parallel()

	const capacity = 4
	occupied := make(chan struct{}, capacity)
	release := make(chan struct{})
	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		occupied <- struct{}{}
		select {
		case <-release:
		case <-r.Context().Done():
		case <-time.After(20 * time.Second):
		}
		w.WriteHeader(http.StatusOK)
	}), Limits{MaxConcurrentStreams: capacity})
	defer close(release)

	pool := newPool(t, PoolConfig{})
	conn := link(t, pool, "cluster-ap", connector)
	if got := conn.Capacity(); got != capacity {
		t.Fatalf("the gateway learned a stream limit of %d, want %d", got, capacity)
	}

	transport := pool.RoundTripper("cluster-ap")
	for i := 0; i < capacity; i++ {
		go func() {
			req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions", strings.NewReader(`{}`))
			if err != nil {
				return
			}
			if resp, err := transport.RoundTrip(req); err == nil {
				_ = resp.Body.Close()
			}
		}()
	}
	for i := 0; i < capacity; i++ {
		select {
		case <-occupied:
		case <-time.After(10 * time.Second):
			t.Fatalf("only %d of %d streams reached the connector", i, capacity)
		}
	}

	req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	resp, err := transport.RoundTrip(req)
	elapsed := time.Since(start)
	if err == nil {
		_ = resp.Body.Close()
		t.Fatal("the tunnel accepted a request past its stream limit")
	}
	if !errors.Is(err, ErrSaturated) {
		t.Fatalf("failed with %v, want ErrSaturated", err)
	}
	if fault := Classify(err); fault != FaultSaturated {
		t.Errorf("classified as %s, want %s", fault, FaultSaturated)
	}
	if elapsed > 250*time.Millisecond {
		t.Errorf("took %s to say there was no capacity; that is latency, not a refusal", elapsed)
	}
}

// TestCancellationReachesTheUpstream follows a caller hanging up all the way
// through: gateway, tunnel, connector handler, and the serving runtime behind
// it.
//
// Without this, a caller who closes their laptop leaves a GPU generating tokens
// nobody will read, and the only thing that stops it is the completion running
// out of output length.
func TestCancellationReachesTheUpstream(t *testing.T) {
	t.Parallel()

	upstreamCanceled := make(chan struct{})
	handlerCanceled := make(chan struct{})
	streaming := make(chan struct{}, 1)

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		flusher := http.NewResponseController(w)
		_, _ = w.Write([]byte("data: tok0\n\n"))
		_ = flusher.Flush()
		select {
		case <-r.Context().Done():
			close(upstreamCanceled)
		case <-time.After(20 * time.Second):
		}
	}))
	defer upstream.Close()

	proxy := proxyTo(upstream.URL)
	// The check is deferred because httputil.ReverseProxy panics with
	// http.ErrAbortHandler when a response it is already streaming is
	// interrupted, which is exactly the case under test.
	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if r.Context().Err() != nil {
				close(handlerCanceled)
			}
		}()
		proxy.ServeHTTP(w, r)
	}), Limits{})

	pool := newPool(t, PoolConfig{})
	link(t, pool, "cluster-ap", connector)

	front := httptest.NewServer(gatewayProxy(pool, "cluster-ap", func(resp *http.Response) error {
		select {
		case streaming <- struct{}{}:
		default:
		}
		return nil
	}))
	defer front.Close()

	ctx, cancel := context.WithCancel(context.Background())
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, front.URL+"/v1/chat/completions",
		strings.NewReader(`{"model":"llama-3-70b","stream":true}`))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		cancel()
		t.Fatalf("round trip: %v", err)
	}
	<-streaming
	line, err := readDataLine(bufio.NewReader(resp.Body))
	if err != nil {
		cancel()
		t.Fatalf("first chunk: %v", err)
	}
	if line != "data: tok0" {
		cancel()
		t.Fatalf("first chunk was %q", line)
	}

	cancel()
	_ = resp.Body.Close()

	select {
	case <-upstreamCanceled:
	case <-time.After(10 * time.Second):
		t.Fatal("the serving runtime behind the connector was never told the caller had gone")
	}
	select {
	case <-handlerCanceled:
	case <-time.After(10 * time.Second):
		t.Fatal("the connector's handler context was never cancelled")
	}
}

// TestDeadPeerIsDetectedAndRemovedFromService covers a tunnel whose far end
// vanished without a FIN: a node that lost power, a NAT that forgot the mapping.
//
// TCP will not notice for a long time, and neither would the gateway: the
// socket stays writable and the pool would keep handing it requests that never
// come back. The PING is the only thing that knows.
//
// The time budget is the assertion, not a safety margin. x/net delays MarkDead
// for a connection that never carried a request by unusedWaitTime, five seconds,
// and a readiness gate counting coverage is counting exactly that connection,
// which is what Conn.watch exists to defeat. A generous budget passes with the
// watcher deleted; this one does not.
func TestDeadPeerIsDetectedAndRemovedFromService(t *testing.T) {
	t.Parallel()

	const noticed = 750 * time.Millisecond
	pool := newPool(t, PoolConfig{
		Limits: Limits{
			ReadIdleTimeout: 100 * time.Millisecond,
			PingTimeout:     100 * time.Millisecond,
		},
		HealthInterval: 50 * time.Millisecond,
	})

	gatewaySide, peerSide := socketPair(t)
	peer := newSilentPeer(t, peerSide, 32)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, err := pool.Adopt(ctx, "cluster-us", gatewaySide)
	if err != nil {
		t.Fatalf("adopt: %v", err)
	}
	if pool.Live("cluster-us") != 1 {
		t.Fatalf("a verified tunnel is not counted as live")
	}

	// The far end goes away without closing anything.
	peer.goSilent()

	select {
	case <-conn.Done():
	case <-time.After(noticed):
		t.Fatalf("a tunnel whose peer vanished was still counted as live after %s", noticed)
	}
	eventually(t, noticed, "the dead tunnel to leave the pool", func() bool {
		return pool.Live("cluster-us") == 0 && len(pool.Coverage()) == 0
	})

	req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	if resp, err := pool.RoundTripper("cluster-us").RoundTrip(req); err == nil {
		_ = resp.Body.Close()
		t.Fatal("a request was sent down a dead tunnel")
	} else if fault := Classify(err); fault != FaultNoTunnel {
		t.Errorf("classified as %s, want %s: %v", fault, FaultNoTunnel, err)
	}
}

// TestAConnectorNoticesAGatewayThatVanished is the mirror of the test above, and
// the half that is easy to forget.
//
// A tunnel is one socket and either end can be the one that disappears. If the
// gateway pod is killed while a connector holds a tunnel to it, the
// connector's socket stays writable, its ServeConn never returns, and it never
// redials, so the cluster keeps a phantom tunnel and loses coverage the gateway
// cannot get back. The connector has no accept loop and no idle timeout by
// design; ReadIdleTimeout and PingTimeout are the only things that notice.
func TestAConnectorNoticesAGatewayThatVanished(t *testing.T) {
	t.Parallel()

	const noticed = 750 * time.Millisecond
	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}), Limits{
		ReadIdleTimeout: 100 * time.Millisecond,
		PingTimeout:     100 * time.Millisecond,
	})

	gatewaySide, connectorSide := socketPair(t)
	served := make(chan struct{})
	go func() {
		defer close(served)
		_ = connector.Serve(connectorSide)
	}()

	// A gateway that handshakes and then stops existing: the socket is never
	// closed, so nothing but a PING will ever tell the connector.
	if _, err := io.WriteString(gatewaySide, http2.ClientPreface); err != nil {
		t.Fatalf("writing the client preface: %v", err)
	}
	framer := http2.NewFramer(gatewaySide, gatewaySide)
	if err := framer.WriteSettings(); err != nil {
		t.Fatalf("writing SETTINGS: %v", err)
	}

	select {
	case <-served:
	case <-time.After(noticed):
		t.Fatalf("the connector was still holding a tunnel to a gateway that vanished after %s; "+
			"it will never redial and the cluster has lost that coverage", noticed)
	}
}

// TestFlowControlWindowsAreSetExplicitly checks the configured limits survive the
// round trip through both ends and are usable, not merely announced.
//
// It is the end-to-end complement to the frame-level assertions in wire_test.go,
// which are what actually pin the numbers. On its own it would prove very little
// about the windows: x/net's client defaults to a 4 MiB stream window and a 1 GiB
// connection window, so a response larger than the spec's 64 KiB completes with
// the configuration deleted too. What it does pin is the connector's stream limit
// reaching the gateway, which is what admission control runs on.
func TestFlowControlWindowsAreSetExplicitly(t *testing.T) {
	t.Parallel()

	limits := Limits{
		MaxConcurrentStreams: 33,
		ConnWindow:           7 << 20,
		StreamWindow:         3 << 20,
		MaxReadFrameSize:     1 << 19,
	}
	body := strings.Repeat("x", 2<<20)
	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(body))
	}), limits)

	pool := newPool(t, PoolConfig{Limits: limits})
	conn := link(t, pool, "cluster-ap", connector)

	if got := conn.Capacity(); got != int(limits.MaxConcurrentStreams) {
		t.Errorf("the connector advertised %d concurrent streams, want %d", got, limits.MaxConcurrentStreams)
	}

	// A response several times the configured frame size, to check the windows are
	// workable and not merely on the wire.
	req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := pool.RoundTripper("cluster-ap").RoundTrip(req)
	if err != nil {
		t.Fatalf("round trip: %v", err)
	}
	defer resp.Body.Close()
	read, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("reading a %d byte response: %v", len(body), err)
	}
	if len(read) != len(body) {
		t.Errorf("read %d bytes, want %d", len(read), len(body))
	}
}

// TestAuthorityIsDerivedFromTheClusterID pins the placeholder authority.
//
// http2's RoundTrip rejects any scheme but http and https, so a tunnel needs a
// synthetic URL. The old pool key in routing.table.go was the serving URL plus
// the credential id; over a tunnel there is no URL, and the cluster is the only
// thing left that identifies where a request is going.
func TestAuthorityIsDerivedFromTheClusterID(t *testing.T) {
	t.Parallel()

	for _, id := range []string{"cluster-ap", "0192F3AE-BEEF_cafe", ""} {
		authority := Authority(id)
		if authority.Scheme != "https" {
			t.Errorf("%q: scheme %q, want https", id, authority.Scheme)
		}
		if !strings.HasSuffix(authority.Host, authoritySuffix) {
			t.Errorf("%q: host %q does not carry the tunnel suffix", id, authority.Host)
		}
		if strings.ContainsAny(authority.Host, "_ ") {
			t.Errorf("%q: host %q is not a usable authority", id, authority.Host)
		}
	}
	if a, b := Authority("cluster-ap"), Authority("cluster-us"); a.Host == b.Host {
		t.Errorf("two clusters share the authority %q", a.Host)
	}
	if _, err := http.NewRequest(http.MethodPost, Authority("cluster-ap").String()+"/v1/chat/completions", nil); err != nil {
		t.Errorf("the authority does not make a valid request URL: %v", err)
	}
}

// TestPoolShutdownDrainsEveryTunnel covers the hazard that cmd/gateway/main.go drains
// the inbound listener and nothing else.
//
// Tunnels arrive on a different listener. Without this, rolling the gateway
// severs every in-flight generation at the far end of a connection the caller
// cannot see, and the caller's own connection is drained politely while the
// answer it is waiting for is thrown away.
func TestPoolShutdownDrainsEveryTunnel(t *testing.T) {
	t.Parallel()

	accepted := make(chan struct{}, 1)
	release := make(chan struct{})
	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		flusher := http.NewResponseController(w)
		_, _ = w.Write([]byte("first "))
		_ = flusher.Flush()
		accepted <- struct{}{}
		select {
		case <-release:
		case <-time.After(20 * time.Second):
		}
		_, _ = w.Write([]byte("last"))
	}), Limits{})

	pool := newPool(t, PoolConfig{})
	link(t, pool, "cluster-ap", connector)
	link(t, pool, "cluster-us", connector)
	if got := len(pool.Clusters()); got != 2 {
		t.Fatalf("pool covers %d clusters, want 2", got)
	}

	req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := pool.RoundTripper("cluster-ap").RoundTrip(req)
	if err != nil {
		t.Fatalf("round trip: %v", err)
	}
	defer resp.Body.Close()
	<-accepted

	shutdown := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		shutdown <- pool.Shutdown(ctx)
	}()

	eventually(t, 5*time.Second, "the pool to stop offering tunnels", func() bool {
		return len(pool.Coverage()) == 0
	})
	if _, err := pool.RoundTripper("cluster-us").RoundTrip(mustRequest(t)); err == nil {
		t.Error("a draining pool accepted a new request")
	}

	close(release)
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("the draining stream was severed: %v", err)
	}
	if string(body) != "first last" {
		t.Errorf("body was %q, want %q", body, "first last")
	}
	if err := <-shutdown; err != nil {
		t.Errorf("shutdown: %v", err)
	}
}

// TestCoverageIsObservedNotWrittenBack checks the Observer is told about tunnel
// coverage and that a replica holding none can tell.
//
// Reachability lives here as a gauge on this process. It does not become a plane
// object and it is not written into observed state, which has exactly one writer:
// ingestion, fed by the operator's sync stream.
func TestCoverageIsObservedNotWrittenBack(t *testing.T) {
	t.Parallel()

	opened := make(chan string, 4)
	closedTunnels := make(chan string, 4)
	pool := newPool(t, PoolConfig{Observer: recordingObserver{opened, closedTunnels}})

	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}), Limits{})

	if got := pool.Live("cluster-ap"); got != 0 {
		t.Fatalf("an empty pool reports %d live tunnels", got)
	}
	conn := link(t, pool, "cluster-ap", connector)
	if got := <-opened; got != "cluster-ap" {
		t.Errorf("observer saw %q open", got)
	}
	if got := pool.Live("cluster-ap"); got != 1 {
		t.Errorf("pool reports %d live tunnels, want 1", got)
	}

	if err := conn.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	select {
	case got := <-closedTunnels:
		if got != "cluster-ap" {
			t.Errorf("observer saw %q close", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("observer was never told a tunnel closed")
	}
	eventually(t, 5*time.Second, "coverage to drop to nothing", func() bool {
		return pool.Live("cluster-ap") == 0
	})
}

type recordingObserver struct {
	opened chan string
	closed chan string
}

func (o recordingObserver) TunnelOpened(clusterID string, _ int) {
	select {
	case o.opened <- clusterID:
	default:
	}
}

func (o recordingObserver) TunnelClosed(clusterID string, _ int) {
	select {
	case o.closed <- clusterID:
	default:
	}
}

// The three hops on the inference path each arm a response-header timer, and
// only the smallest one is ever observed. So a fix that raises the outer hops
// without raising the innermost is not a fix: the innermost still fires first
// and the caller still gets the same 502 at the same wall.
func TestEveryHopInwardsWaitsLongerThanTheOneOutsideIt(t *testing.T) {
	for _, tc := range []struct {
		name   string
		budget time.Duration
	}{
		{"the default budget", DefaultResponseHeaderTimeout},
		{"a budget an operator raised", 90 * time.Minute},
		{"a budget an operator lowered", 10 * time.Minute},
	} {
		t.Run(tc.name, func(t *testing.T) {
			inner := InnerHopTimeout(tc.budget)
			if inner <= tc.budget {
				t.Fatalf("inner hop waits %s against an outer %s; the inner one fires first", inner, tc.budget)
			}
			if inner != tc.budget+InnerHopMargin {
				t.Errorf("inner hop = %s, want the budget plus one margin (%s)", inner, tc.budget+InnerHopMargin)
			}
		})
	}
}

// Zero is "nothing was configured", not "no bound". A hop that read it as no
// bound would hold a stream slot for a wedged frontend forever; one that read it
// as a small number would fire before the hops outside it.
func TestAnUnsetInnerBudgetFallsBackAboveTheDefault(t *testing.T) {
	if got := InnerHopTimeout(0); got != DefaultResponseHeaderTimeout+InnerHopMargin {
		t.Fatalf("InnerHopTimeout(0) = %s, want %s", got, DefaultResponseHeaderTimeout+InnerHopMargin)
	}
	if InnerHopTimeout(0) <= DefaultResponseHeaderTimeout {
		t.Fatal("an unconfigured inner hop does not outlast the default outer one")
	}
}

// A long-context generation with no max_tokens cap can run for tens of minutes,
// and non-streamed, all of it sits inside this timer. This pins the floor so
// that a later tidy-up cannot walk it back without saying so.
func TestTheDefaultCoversALongNonStreamedGeneration(t *testing.T) {
	if DefaultResponseHeaderTimeout < 30*time.Minute {
		t.Fatalf("default is %s; a long non-streamed generation does not finish in that",
			DefaultResponseHeaderTimeout)
	}
}

// The pool takes the budget it is given and nothing rounds it, because the
// gateway's two paths into a cluster (the tunnel and a published address) have
// to present the same wall to a caller.
func TestThePoolKeepsTheBudgetItIsGiven(t *testing.T) {
	pool := newPool(t, PoolConfig{ResponseHeaderTimeout: 90 * time.Minute})
	if pool.cfg.ResponseHeaderTimeout != 90*time.Minute {
		t.Fatalf("pool budget = %s, want the 90m it was configured with", pool.cfg.ResponseHeaderTimeout)
	}
	unset := newPool(t, PoolConfig{})
	if unset.cfg.ResponseHeaderTimeout != DefaultResponseHeaderTimeout {
		t.Fatalf("unconfigured pool budget = %s, want the default %s",
			unset.cfg.ResponseHeaderTimeout, DefaultResponseHeaderTimeout)
	}
}
