package tunnel

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/net/http2"
)

// TestUnprocessedRequestsAreRetriedOnASiblingTunnel is the hazard this package
// exists to close, for a connector that was drained, not killed.
//
// shouldRetryRequest lives on http2.Transport.RoundTrip and its sentinels are
// unexported, so a bare *http2.ClientConn turns every in-flight-but-unprocessed
// request into a 502 the moment a connector restarts. The peer here announces
// GOAWAY with LastStreamID 0 ("I processed nothing") and then holds the socket
// open the way a draining pod does, which is precisely the case a real connector
// produces for streams whose headers it had not read when it was told to stop.
//
// The requests are gateway-shaped, which is the essential detail: a body with
// no GetBody on it is one HTTP/2 refuses to replay, so this is also the test that
// fails if makeReplayable stops working. Build them with http.NewRequest over a
// strings.Reader instead and net/http fills GetBody in for you, the pool's
// buffering never runs, and the test passes for the wrong reason.
func TestUnprocessedRequestsAreRetriedOnASiblingTunnel(t *testing.T) {
	t.Parallel()

	const inFlight = 12
	served := newCounter()
	survivor := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(body), `"model"`) {
			http.Error(w, "the replayed body was truncated", http.StatusBadRequest)
			return
		}
		served.add(r.Header.Get("X-Request-Id"))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("served-by-survivor"))
	}), Limits{})

	pool := newPool(t, PoolConfig{})
	survivorConn := link(t, pool, "cluster-ap", survivor)

	// The doomed tunnel speaks HTTP/2 and answers nothing. Reserving slots on the
	// survivor makes the pool's least-loaded pick send everything to the doomed
	// one, deterministically, for as long as it is live.
	gatewaySide, peerSide := socketPair(t)
	peer := newSilentPeer(t, peerSide, 64)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := pool.Adopt(ctx, "cluster-ap", gatewaySide); err != nil {
		t.Fatalf("adopt doomed tunnel: %v", err)
	}
	occupy(survivorConn, inFlight+4)

	transport := pool.RoundTripper("cluster-ap")
	results := make(chan error, inFlight)
	for i := 0; i < inFlight; i++ {
		id := fmt.Sprintf("req-%02d", i)
		req := gatewayShapedRequest(t, "/v1/chat/completions", `{"model":"llama-3-70b","messages":[]}`)
		req.Header.Set("X-Request-Id", id)
		go func() {
			resp, err := transport.RoundTrip(req)
			if err != nil {
				results <- fmt.Errorf("%s: %w (%s)", id, err, Classify(err))
				return
			}
			defer resp.Body.Close()
			body, err := io.ReadAll(resp.Body)
			if err != nil {
				results <- fmt.Errorf("%s: reading body: %w", id, err)
				return
			}
			if resp.StatusCode != http.StatusOK || string(body) != "served-by-survivor" {
				results <- fmt.Errorf("%s: status %d body %q", id, resp.StatusCode, body)
				return
			}
			results <- nil
		}()
	}

	peer.awaitRequests(t, inFlight)
	if served.total() != 0 {
		t.Fatalf("the survivor served %d requests before the restart", served.total())
	}

	peer.drainGracefully(t)

	for i := 0; i < inFlight; i++ {
		select {
		case err := <-results:
			if err != nil {
				t.Errorf("request failed instead of being retried: %v", err)
			}
		case <-time.After(20 * time.Second):
			t.Fatalf("only %d of %d requests finished", i, inFlight)
		}
	}

	if served.total() != inFlight {
		t.Errorf("the survivor served %d requests, want %d", served.total(), inFlight)
	}
	for i := 0; i < inFlight; i++ {
		id := fmt.Sprintf("req-%02d", i)
		if got := served.get(id); got != 1 {
			t.Errorf("%s was served %d times, want exactly once", id, got)
		}
	}
	eventually(t, 5*time.Second, "the doomed tunnel to leave the pool", func() bool {
		return pool.Live("cluster-ap") == 1
	})
}

// TestARequestWithoutGetBodyCannotBeRetried pins the premise makeReplayable is
// built on, and only that.
//
// It goes to pool.t2 directly, underneath clusterTransport.RoundTrip, so nothing
// in the pool has touched the request: what it proves is that x/net refuses to
// replay a body it cannot rewind, which is the reason the pool buffers one. It is
// not the regression guard for makeReplayable: it cannot be, because it bypasses
// it. That guard is TestUnprocessedRequestsAreRetriedOnASiblingTunnel and its
// abrupt-death sibling, both of which send gateway-shaped requests through the
// exported RoundTripper.
//
// If x/net ever learns to replay without GetBody, this test fails and the pool's
// buffering can be reconsidered.
func TestARequestWithoutGetBodyCannotBeRetried(t *testing.T) {
	t.Parallel()

	survivor := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.WriteHeader(http.StatusOK)
	}), Limits{})

	pool := newPool(t, PoolConfig{})
	survivorConn := link(t, pool, "cluster-ap", survivor)

	gatewaySide, peerSide := socketPair(t)
	peer := newSilentPeer(t, peerSide, 64)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := pool.Adopt(ctx, "cluster-ap", gatewaySide); err != nil {
		t.Fatalf("adopt doomed tunnel: %v", err)
	}
	occupy(survivorConn, 8)

	req, err := http.NewRequest(http.MethodPost, "https://cluster.tunnel.hangar.internal/v1/chat/completions",
		io.NopCloser(strings.NewReader(`{"model":"llama-3-70b"}`)))
	if err != nil {
		t.Fatal(err)
	}
	req.GetBody = nil
	req = req.WithContext(context.WithValue(context.Background(), clusterKey{}, "cluster-ap"))

	done := make(chan error, 1)
	go func() {
		resp, err := pool.t2.RoundTrip(req)
		if err == nil {
			_ = resp.Body.Close()
		}
		done <- err
	}()

	peer.awaitRequests(t, 1)
	peer.drainGracefully(t)

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("a request with no GetBody was retried; makeReplayable is no longer needed and this test should go")
		}
		if !strings.Contains(err.Error(), "GetBody") {
			t.Fatalf("failed for the wrong reason: %v", err)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("the request neither failed nor completed")
	}
}

// TestRequestsAConnectorWasKilledHoldingAreNotResent is the death that
// actually happens.
//
// A drained connector says GOAWAY and x/net's own retry takes it from there. A
// SIGKILLed one, an OOMKilled one, or one whose node was lost says nothing at
// all, and the two shapes that silence takes on the wire are both here: an RST,
// which reaches the gateway as ECONNRESET or EPIPE, and a FIN, which reaches it
// as an unexpected EOF.
//
// Either way the gateway cannot know whether the connector had already
// forwarded a request it was holding. A completion POST that was forwarded is
// generating, and billed, on the cluster; sending it again would run it twice.
// So a non-idempotent request that reached the connector fails as churn and is
// not resent, while an idempotent one (a safe method, or a POST carrying an
// Idempotency-Key) is retried down the surviving tunnel exactly once.
func TestRequestsAConnectorWasKilledHoldingAreNotResent(t *testing.T) {
	t.Parallel()

	deaths := []struct {
		name string
		kill func(*silentPeer, *testing.T)
	}{
		{"the socket is reset", (*silentPeer).dieAbruptly},
		{"the socket is closed with no GOAWAY", (*silentPeer).vanish},
	}
	requests := []struct {
		name    string
		build   func(t *testing.T) *http.Request
		retried bool
	}{
		{"a completion POST", func(t *testing.T) *http.Request {
			return gatewayShapedRequest(t, "/v1/chat/completions", `{"model":"llama-3-70b","messages":[]}`)
		}, false},
		{"a POST with an idempotency key", func(t *testing.T) *http.Request {
			req := gatewayShapedRequest(t, "/v1/chat/completions", `{"model":"llama-3-70b","messages":[]}`)
			req.Header.Set("Idempotency-Key", "k")
			return req
		}, true},
		{"a GET", func(t *testing.T) *http.Request {
			req, err := http.NewRequest(http.MethodGet, "https://ignored/v1/models", nil)
			if err != nil {
				t.Fatal(err)
			}
			return req
		}, true},
	}
	for _, death := range deaths {
		for _, request := range requests {
			t.Run(death.name+"/"+request.name, func(t *testing.T) {
				t.Parallel()
				killConnectorHolding(t, death.kill, request.build, request.retried)
			})
		}
	}
}

func killConnectorHolding(t *testing.T, kill func(*silentPeer, *testing.T), build func(*testing.T) *http.Request, retried bool) {
	t.Helper()

	const inFlight = 12
	served := newCounter()
	survivor := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.Method == http.MethodPost && !strings.Contains(string(body), `"model"`) {
			http.Error(w, "the replayed body was truncated", http.StatusBadRequest)
			return
		}
		served.add(r.Header.Get("X-Request-Id"))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("served-by-survivor"))
	}), Limits{})

	retries := &retryLog{outcomes: newCounter()}
	pool := newPool(t, PoolConfig{Observer: retries})
	survivorConn := link(t, pool, "cluster-ap", survivor)

	gatewaySide, peerSide := socketPair(t)
	peer := newSilentPeer(t, peerSide, 64)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := pool.Adopt(ctx, "cluster-ap", gatewaySide); err != nil {
		t.Fatalf("adopt doomed tunnel: %v", err)
	}
	occupy(survivorConn, inFlight+4)

	transport := pool.RoundTripper("cluster-ap")
	results := make(chan error, inFlight)
	for i := 0; i < inFlight; i++ {
		id := fmt.Sprintf("req-%02d", i)
		req := build(t)
		req.Header.Set("X-Request-Id", id)
		go func() {
			resp, err := transport.RoundTrip(req)
			if err != nil {
				results <- fmt.Errorf("%s: %w", id, err)
				return
			}
			defer resp.Body.Close()
			body, err := io.ReadAll(resp.Body)
			if err != nil {
				results <- fmt.Errorf("%s: reading body: %w", id, err)
				return
			}
			if resp.StatusCode != http.StatusOK || string(body) != "served-by-survivor" {
				results <- fmt.Errorf("%s: status %d body %q", id, resp.StatusCode, body)
				return
			}
			results <- nil
		}()
	}

	peer.awaitRequests(t, inFlight)
	if served.total() != 0 {
		t.Fatalf("the survivor served %d requests before the connector was killed", served.total())
	}

	kill(peer, t)

	for i := 0; i < inFlight; i++ {
		select {
		case err := <-results:
			switch {
			case retried && err != nil:
				t.Errorf("an idempotent request failed instead of being retried: %v", err)
			case !retried && err == nil:
				t.Error("a request the killed connector may have forwarded was sent again")
			case !retried && Classify(err) != FaultChurn:
				t.Errorf("classified as %s, want %s: %v", Classify(err), FaultChurn, err)
			}
		case <-time.After(20 * time.Second):
			t.Fatalf("only %d of %d requests finished", i, inFlight)
		}
	}
	want := 0
	outcome := RetryWritten
	if retried {
		want = 1
		outcome = RetryResent
	}
	for i := 0; i < inFlight; i++ {
		id := fmt.Sprintf("req-%02d", i)
		if got := served.get(id); got != want {
			t.Errorf("%s was served %d times by the survivor, want %d", id, got, want)
		}
	}
	if got := retries.outcomes.snapshot(); len(got) != 1 || got[outcome] != inFlight {
		t.Errorf("retry outcomes = %v, want %d %q", got, inFlight, outcome)
	}
	eventually(t, 5*time.Second, "the killed tunnel to leave the pool", func() bool {
		return pool.Live("cluster-ap") == 1
	})
}

// TestTheRetryRuleAfterATunnelDies pins which requests may be sent down another
// tunnel after the one they were on died with no GOAWAY.
func TestTheRetryRuleAfterATunnelDies(t *testing.T) {
	t.Parallel()

	reset := &net.OpError{Op: "read", Err: syscall.ECONNRESET}
	post := func(header string) *http.Request {
		req := gatewayShapedRequest(t, "/v1/chat/completions", `{"model":"m"}`)
		if header != "" {
			req.Header[header] = []string{"k"}
		}
		return req
	}
	get, err := http.NewRequest(http.MethodGet, "https://ignored/v1/models", nil)
	if err != nil {
		t.Fatal(err)
	}
	canceledCtx, cancel := context.WithCancel(context.Background())
	cancel()

	for _, tc := range []struct {
		name  string
		req   *http.Request
		err   error
		wrote bool
		want  bool
	}{
		{"a POST that never left", post(""), reset, false, true},
		{"a POST whose headers were written", post(""), reset, true, false},
		{"a POST with Idempotency-Key", post("Idempotency-Key"), reset, true, true},
		{"a POST with X-Idempotency-Key", post("X-Idempotency-Key"), reset, true, true},
		{"a GET whose headers were written", get, reset, true, true},
		{"a caller that hung up", get.WithContext(canceledCtx), reset, false, false},
		{"an error that is not the tunnel dying", get, errors.New("boom"), false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := retryAfterTunnelDeath(tc.req, tc.err, tc.wrote); got != tc.want {
				t.Errorf("retryAfterTunnelDeath = %v, want %v", got, tc.want)
			}
		})
	}
}

// TestAKilledTunnelIsChurnNotAClusterFailure pins the classification either side
// of the syscall boundary.
//
// A reset or a broken pipe on a tunnel socket is the near end of the tunnel
// dying: the connector proxies to the cluster inside its own process and reports
// a cluster that will not answer as a status, so these errors cannot be the
// cluster. Reading them as FaultUpstream is what made a crashed connector look
// like an outage. Reading a genuine upstream failure as churn would be the same
// mistake pointed the other way, and the second half of this test is what stops
// that.
func TestAKilledTunnelIsChurnNotAClusterFailure(t *testing.T) {
	t.Parallel()

	tunnelDeaths := []error{
		&net.OpError{Op: "read", Net: "tcp", Err: syscall.ECONNRESET},
		&net.OpError{Op: "write", Net: "tcp", Err: syscall.EPIPE},
		fmt.Errorf("proxying: %w", &net.OpError{Op: "read", Net: "tcp", Err: syscall.ECONNRESET}),
		errors.New("http2: client connection force closed via ClientConn.Close"),
		errors.New("http2: client conn is closed"),
		errors.New("http2: client connection lost"),
		io.ErrUnexpectedEOF,
	}
	for _, err := range tunnelDeaths {
		if got := Classify(err); got != FaultChurn {
			t.Errorf("Classify(%v) = %s, want %s", err, got, FaultChurn)
		}
		if !isTunnelDeath(err) {
			t.Errorf("%v is not treated as the tunnel dying, so a request that hit it is not retried", err)
		}
	}

	notTunnelDeaths := []error{
		errors.New("dial tcp 10.0.0.1:8000: connect: connection refused"),
		&net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED},
	}
	for _, err := range notTunnelDeaths {
		if isTunnelDeath(err) {
			t.Errorf("%v is treated as the tunnel dying, so a cluster failure would be retried and then hidden", err)
		}
		if got := Classify(err); got != FaultUpstream {
			t.Errorf("Classify(%v) = %s, want %s", err, got, FaultUpstream)
		}
	}
}

// TestARetryPrefersATunnelThisRequestHasNotTried is the pick that makes the retry
// worth having.
//
// Least-loaded is the right rule for a fresh request and the wrong one for a
// retry. The moment a connector is killed, every stream on its tunnel fails, so
// for a few milliseconds the corpse has the fewest streams of any tunnel to the
// cluster, and a retry that asks for the least-loaded tunnel is handed the thing
// that just failed it, until the retry budget runs out. That was a real failure
// under load about once in thirty runs; asserting it through the pick instead
// of through a race is what makes it a test.
func TestARetryPrefersATunnelThisRequestHasNotTried(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	pool := newPool(t, PoolConfig{})
	first := link(t, pool, "cluster-ap", newConnector(t, handler, Limits{}))
	second := link(t, pool, "cluster-ap", newConnector(t, handler, Limits{}))
	// first is now strictly the least loaded of the two, for every pick.
	occupy(second, 4)

	req := gatewayShapedRequest(t, "/v1/chat/completions", `{"model":"llama-3-70b"}`)
	ctx := context.WithValue(req.Context(), clusterKey{}, "cluster-ap")
	ctx = context.WithValue(ctx, attemptKey{}, &attempts{})
	req = req.WithContext(ctx)

	picks := make([]*http2.ClientConn, 0, 3)
	for attempt := 1; attempt <= 3; attempt++ {
		cc, err := pool.GetClientConn(req, "ignored")
		if err != nil {
			t.Fatalf("attempt %d found no tunnel: %v", attempt, err)
		}
		picks = append(picks, cc)
	}

	if picks[0] != first.cc {
		t.Error("the first attempt did not go to the least-loaded tunnel")
	}
	if picks[1] != second.cc {
		t.Error("the retry went back to the tunnel that had just failed this request, " +
			"which is where a killed connector sends it")
	}
	if picks[2] != first.cc {
		t.Error("with both tunnels already tried, the pick did not fall back to the least loaded")
	}
}

// TestKillingTheOnlyTunnelStillReadsAsChurn covers what a failed retry reports.
//
// The retry needs somewhere to go, and when the killed tunnel was the cluster's
// only one there is nowhere: the second attempt fails with ErrNoTunnel. Returning
// that would be a lie about which thing broke: ErrNoTunnel means nothing was
// attempted, and this request had already been written to a socket. It would also
// be the more flattering lie, since a cluster with no connector attached is
// capacity instead of a failure.
func TestKillingTheOnlyTunnelStillReadsAsChurn(t *testing.T) {
	t.Parallel()

	pool := newPool(t, PoolConfig{})
	gatewaySide, peerSide := socketPair(t)
	peer := newSilentPeer(t, peerSide, 64)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := pool.Adopt(ctx, "cluster-ap", gatewaySide); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	transport := pool.RoundTripper("cluster-ap")
	done := make(chan error, 1)
	go func() {
		resp, err := transport.RoundTrip(gatewayShapedRequest(t, "/v1/chat/completions",
			`{"model":"llama-3-70b"}`))
		if err == nil {
			_ = resp.Body.Close()
		}
		done <- err
	}()

	peer.awaitRequests(t, 1)
	peer.dieAbruptly(t)

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("a request completed on a tunnel that was killed under it")
		}
		if fault := Classify(err); fault != FaultChurn {
			t.Errorf("classified as %s, want %s: %v", fault, FaultChurn, err)
		}
		if errors.Is(err, ErrNoTunnel) {
			t.Errorf("reported as nothing having been attempted: %v", err)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("the request neither failed nor completed")
	}
}

// TestAStreamAlreadyReachingTheCallerIsNotRetriedAfterAnAbruptDeath is the
// boundary the pool's own retry must never cross.
//
// The retry sits on the error return of RoundTrip, which is the pre-header case
// by construction. Once headers are out the caller holds a *http.Response and
// bytes have already reached them, and re-sending the request then would splice a
// second generation onto the end of the first: one answer, two completions, no
// way for the caller to tell. An RST is the tempting case to get wrong, because
// the pool now knows that error means the tunnel died and could retry it.
func TestAStreamAlreadyReachingTheCallerIsNotRetriedAfterAnAbruptDeath(t *testing.T) {
	t.Parallel()

	streaming := make(chan struct{}, 1)
	release := make(chan struct{})
	defer close(release)
	served := newCounter()

	stream := func(who string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			served.add(who)
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			flusher := http.NewResponseController(w)
			fmt.Fprintf(w, "data: first-from-%s\n\n", who)
			_ = flusher.Flush()
			select {
			case streaming <- struct{}{}:
			default:
			}
			select {
			case <-release:
			case <-r.Context().Done():
			case <-time.After(20 * time.Second):
			}
			fmt.Fprintf(w, "data: last-from-%s\n\n", who)
			_ = flusher.Flush()
		})
	}

	doomed := newConnector(t, stream("doomed"), Limits{})
	survivor := newConnector(t, stream("survivor"), Limits{})

	pool := newPool(t, PoolConfig{})
	_, kill := linkKillable(t, pool, "cluster-ap", doomed)
	survivorConn := link(t, pool, "cluster-ap", survivor)
	occupy(survivorConn, 8)

	transport := pool.RoundTripper("cluster-ap")
	resp, err := transport.RoundTrip(gatewayShapedRequest(t, "/v1/chat/completions",
		`{"model":"llama-3-70b","stream":true}`))
	if err != nil {
		t.Fatalf("round trip: %v", err)
	}
	defer resp.Body.Close()

	reader := bufio.NewReader(resp.Body)
	line, err := readDataLine(reader)
	if err != nil {
		t.Fatalf("first chunk: %v", err)
	}
	if line != "data: first-from-doomed" {
		t.Fatalf("first chunk was %q", line)
	}
	<-streaming

	kill()

	rest, err := io.ReadAll(reader)
	if err == nil {
		t.Fatalf("the body completed after its connector was killed, having read %q", rest)
	}
	if fault := Classify(err); fault != FaultChurn {
		t.Errorf("the severed body read classified as %s, want %s: %v", fault, FaultChurn, err)
	}
	if strings.Contains(string(rest), "survivor") {
		t.Errorf("the stream was silently restarted on another tunnel: %q", rest)
	}
	if got := served.get("survivor"); got != 0 {
		t.Errorf("the survivor served the already-streaming request %d times, want 0", got)
	}
	if got := served.get("doomed"); got != 1 {
		t.Errorf("the doomed connector served the request %d times, want 1", got)
	}
}

// TestAStreamAlreadyReachingTheCallerIsNotRetried is the other half of the
// retry contract.
//
// Once response headers are out, RoundTrip has returned and the bytes are on
// their way to a caller. Replaying the request then would send a second
// generation down the same response: the caller would see one answer, then
// another, spliced. HTTP/2 will not do that, and this test is what says so.
func TestAStreamAlreadyReachingTheCallerIsNotRetried(t *testing.T) {
	t.Parallel()

	release := make(chan struct{})
	streaming := make(chan struct{}, 1)
	served := newCounter()

	stream := func(who string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			served.add(who)
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			flusher := http.NewResponseController(w)
			fmt.Fprintf(w, "data: first-from-%s\n\n", who)
			_ = flusher.Flush()
			select {
			case streaming <- struct{}{}:
			default:
			}
			select {
			case <-release:
			case <-r.Context().Done():
			case <-time.After(20 * time.Second):
			}
			fmt.Fprintf(w, "data: last-from-%s\n\n", who)
			_ = flusher.Flush()
		})
	}

	doomed := newConnector(t, stream("doomed"), Limits{})
	survivor := newConnector(t, stream("survivor"), Limits{})

	pool := newPool(t, PoolConfig{})
	link(t, pool, "cluster-ap", doomed)
	survivorConn := link(t, pool, "cluster-ap", survivor)
	occupy(survivorConn, 8)

	transport := pool.RoundTripper("cluster-ap")
	req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions",
		strings.NewReader(`{"model":"llama-3-70b","stream":true}`))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := transport.RoundTrip(req)
	if err != nil {
		t.Fatalf("round trip: %v", err)
	}
	defer resp.Body.Close()

	reader := bufio.NewReader(resp.Body)
	line, err := readDataLine(reader)
	if err != nil {
		t.Fatalf("first chunk: %v", err)
	}
	if line != "data: first-from-doomed" {
		t.Fatalf("first chunk was %q", line)
	}
	<-streaming

	if err := doomed.Close(); err != nil {
		t.Fatalf("closing the doomed connector: %v", err)
	}

	rest, err := io.ReadAll(reader)
	if err == nil {
		t.Fatalf("the body completed after its connector was killed, having read %q", rest)
	}
	if fault := Classify(err); fault != FaultChurn {
		t.Errorf("the severed body read classified as %s, want %s: %v", fault, FaultChurn, err)
	}
	if strings.Contains(string(rest), "survivor") {
		t.Errorf("the stream was silently restarted on another tunnel: %q", rest)
	}
	if got := served.get("survivor"); got != 0 {
		t.Errorf("the survivor served the already-streaming request %d times, want 0", got)
	}
	if got := served.get("doomed"); got != 1 {
		t.Errorf("the doomed connector served the request %d times, want 1", got)
	}
	close(release)
}

// TestGracefulConnectorShutdownDrainsInsteadOfSevering is the rollout case.
//
// A connector is a Deployment. When it is asked to stop it must send GOAWAY and
// finish the generations it already accepted, because those are half-written
// answers in front of real callers. Killing the process instead is the previous
// test, and it costs a caller their response.
func TestGracefulConnectorShutdownDrainsInsteadOfSevering(t *testing.T) {
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
	conn := link(t, pool, "cluster-us", connector)

	transport := pool.RoundTripper("cluster-us")
	req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions",
		strings.NewReader(`{"model":"llama-3-70b"}`))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := transport.RoundTrip(req)
	if err != nil {
		t.Fatalf("round trip: %v", err)
	}
	defer resp.Body.Close()
	<-accepted

	drained := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		drained <- connector.Shutdown(ctx)
	}()

	// GOAWAY reaches the gateway before the stream finishes, so the tunnel stops
	// taking new work while the work it has carries on.
	eventually(t, 5*time.Second, "the tunnel to stop accepting new requests", func() bool {
		return !conn.Live()
	})
	if _, err := transport.RoundTrip(mustRequest(t)); err == nil {
		t.Error("a draining tunnel accepted a new request")
	} else if fault := Classify(err); fault != FaultNoTunnel {
		t.Errorf("a request to a draining cluster classified as %s, want %s: %v", fault, FaultNoTunnel, err)
	}

	close(release)
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("the draining stream was severed: %v", err)
	}
	if string(body) != "first last" {
		t.Errorf("body was %q, want %q", body, "first last")
	}
	if err := <-drained; err != nil {
		t.Errorf("drain: %v", err)
	}
}

func mustRequest(t *testing.T) *http.Request {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions",
		strings.NewReader(`{"model":"llama-3-70b"}`))
	if err != nil {
		t.Fatal(err)
	}
	return req
}

// retryLog counts what became of requests whose tunnel died.
type retryLog struct {
	outcomes *counter
}

func (*retryLog) TunnelOpened(string, int) {}
func (*retryLog) TunnelClosed(string, int) {}
func (r *retryLog) TunnelRetry(_ string, outcome string) {
	r.outcomes.add(outcome)
}
