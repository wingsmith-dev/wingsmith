package agent

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/agent/internal/dynamo"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// TestTunnelCarriesARequestToTheClusterFrontend is the whole path in one test: the supervisor dials
// out, the gateway adopts the connection into the gateway's pool, and a request driven through
// the pool's RoundTripper comes back out of the cluster's own frontend.
func TestTunnelCarriesARequestToTheClusterFrontend(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"path":"` + r.URL.Path + `"}`))
	}))
	defer upstream.Close()

	resolver, err := dynamo.New(dynamo.Config{Endpoint: upstream.URL})
	if err != nil {
		t.Fatalf("dynamo.New: %v", err)
	}
	server, err := tunnel.NewConnector(tunnel.ConnectorConfig{Handler: dynamo.NewHandler(resolver, 0)})
	if err != nil {
		t.Fatalf("tunnel.NewConnector: %v", err)
	}

	pool, err := tunnel.NewPool(tunnel.PoolConfig{})
	if err != nil {
		t.Fatalf("tunnel.NewPool: %v", err)
	}
	defer func() { _ = pool.Close() }()

	const clusterID = "clu_test"
	tr := startGateway(t, &gateway{accepted: func(conn net.Conn) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := pool.Adopt(ctx, clusterID, conn); err != nil {
			t.Errorf("Adopt: %v", err)
		}
	}})

	supervisor, stop := runSupervisor(t, tr.addr, server, nil)
	defer stop()

	eventually(t, "the gateway to hold a tunnel", func() bool { return pool.Live(clusterID) > 0 })
	if err := supervisor.Ready(); err != nil {
		t.Fatalf("Ready: %v", err)
	}

	client := &http.Client{Transport: pool.RoundTripper(clusterID)}
	resp, err := client.Get(tunnel.Authority(clusterID).String() + "/v1/chat/completions")
	if err != nil {
		t.Fatalf("request over the tunnel: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, body = %s", resp.StatusCode, body)
	}
	if !strings.Contains(string(body), `"path":"/v1/chat/completions"`) {
		t.Errorf("body = %s, want the path the frontend saw", body)
	}
}

// A gateway that goes away is the ordinary case — it is a StatefulSet that gets rolled — so the
// agent has to come back on its own.
func TestSupervisorReconnectsAfterTheGatewayDrops(t *testing.T) {
	server := &blockingServer{}
	var handed atomic.Int64
	conns := make(chan net.Conn, 4)
	tr := startGateway(t, &gateway{accepted: func(conn net.Conn) {
		handed.Add(1)
		conns <- conn
	}})

	supervisor, stop := runSupervisor(t, tr.addr, server, nil)
	defer stop()

	first := <-conns
	eventually(t, "the first tunnel to be counted", func() bool { return supervisor.Tunnels() == 1 })
	_ = first.Close()

	eventually(t, "a redial after the drop", func() bool { return handed.Load() >= 2 })
	eventually(t, "the replacement tunnel to be counted", func() bool { return supervisor.Tunnels() == 1 })
}

// Readiness is the only thing that says this agent can carry a request, so it has to follow the
// tunnels rather than the process. Driven through a stub dialler: a real socket cannot be made to
// fail and then succeed on cue.
func TestSupervisorReadinessFollowsTheTunnels(t *testing.T) {
	var reachable atomic.Bool
	var mu sync.Mutex
	var opened []net.Conn

	dial := func(ctx context.Context, target string, _ []string) (net.Conn, Peer, error) {
		if !reachable.Load() {
			return nil, Peer{}, errors.New("connection refused")
		}
		ours, theirs := net.Pipe()
		mu.Lock()
		opened = append(opened, theirs)
		mu.Unlock()
		return ours, Peer{ID: "gw-0", Total: 1}, nil
	}

	supervisor, err := NewSupervisor(SupervisorConfig{
		Gateway:         "gw.gw:9443",
		ConnsPerGateway: 1,
		Dial:            dial,
		Server:          &blockingServer{},
		BackoffMin:      time.Millisecond,
		BackoffMax:      2 * time.Millisecond,
		Metrics:         NewMetrics(),
		Log:             quietLogger(),
	})
	if err != nil {
		t.Fatalf("NewSupervisor: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		supervisor.Run(ctx)
	}()
	defer func() {
		cancel()
		mu.Lock()
		for _, conn := range opened {
			_ = conn.Close()
		}
		mu.Unlock()
		<-done
	}()

	if err := supervisor.Ready(); err == nil {
		t.Fatal("Ready with no tunnel returned nil")
	} else if !strings.Contains(err.Error(), "gw.gw:9443") {
		t.Errorf("Ready error %q does not name the gateway it cannot reach", err)
	}

	reachable.Store(true)
	eventually(t, "readiness once a tunnel is up", func() bool { return supervisor.Ready() == nil })

	reachable.Store(false)
	mu.Lock()
	for _, conn := range opened {
		_ = conn.Close()
	}
	mu.Unlock()
	eventually(t, "readiness to fall away with the tunnel", func() bool { return supervisor.Ready() != nil })
}

// The claim this change exists to make: an agent told one address ends up holding a tunnel to every
// replica behind it, without ever having been told what they are.
func TestAgentCoversEveryReplicaBehindOneAddress(t *testing.T) {
	server := &blockingServer{}
	gw := startGateway(t, &gateway{
		ids:      []string{"hangar-gateway-0", "hangar-gateway-1", "hangar-gateway-2"},
		accepted: func(net.Conn) {},
	})

	metrics := NewMetrics()
	supervisor, stop := runSupervisor(t, gw.addr, server, metrics)
	defer stop()

	eventually(t, "a tunnel to all three replicas", func() bool {
		coverage := supervisor.Coverage()
		if len(coverage) != 3 {
			return false
		}
		for _, live := range coverage {
			if live != 1 {
				return false
			}
		}
		return true
	})
	if got := supervisor.Expected(); got != 3 {
		t.Errorf("Expected = %d, want the 3 the gateway reported", got)
	}
	if got := gaugeValue(t, metrics, "hangar_agent_gateways_expected", nil); got != 3 {
		t.Errorf("gateways_expected = %v, want 3", got)
	}
}

// A dial that lands somewhere already covered is how coverage is reached at all, so it must not read
// as a failure — the alert on dial failures would fire on every healthy startup if it did.
func TestALandingOnACoveredReplicaIsNotAFailure(t *testing.T) {
	server := &blockingServer{}
	// Two replicas, but the load balancer sends the first two dials to the same one. The second is
	// the duplicate, and the third dial is what finally reaches gateway-1.
	gw := startGateway(t, &gateway{
		ids:      []string{"hangar-gateway-0", "hangar-gateway-0", "hangar-gateway-1"},
		replicas: 2,
		accepted: func(net.Conn) {},
	})

	metrics := NewMetrics()
	supervisor, stop := runSupervisor(t, gw.addr, server, metrics)
	defer stop()

	eventually(t, "both replicas covered despite the duplicate", func() bool {
		return len(supervisor.Coverage()) == 2
	})
	if got := counterValue(t, metrics, "hangar_agent_dial_duplicates_total", map[string]string{
		"gateway": "hangar-gateway-0",
	}); got < 1 {
		t.Errorf("duplicates for gateway-0 = %v, want at least the one that was rejected", got)
	}
	for _, reason := range []string{reasonDial, reasonUnauthorized, reasonHandshake, reasonRefused} {
		if got := counterValue(t, metrics, "hangar_agent_dial_failures_total",
			map[string]string{"reason": reason}); got != 0 {
			t.Errorf("dial failures for %s = %v, want 0 — a duplicate is not a failure", reason, got)
		}
	}
}

// Partial coverage is what the per-replica reporting exists for: a gateway that says it runs two
// replicas while only one ever answers leaves this agent ready and still short of the gateway.
func TestSupervisorReportsPartialCoverage(t *testing.T) {
	server := &blockingServer{}
	gw := startGateway(t, &gateway{ids: []string{"hangar-gateway-0"}, replicas: 2, accepted: func(net.Conn) {}})

	supervisor, stop := runSupervisor(t, gw.addr, server, NewMetrics())
	defer stop()

	eventually(t, "coverage of the replica that answers", func() bool {
		return supervisor.Coverage()["hangar-gateway-0"] == 1
	})
	if err := supervisor.Ready(); err != nil {
		t.Errorf("Ready = %v, want ready on partial coverage", err)
	}
	if reached, want := len(supervisor.Coverage()), supervisor.Expected(); reached >= want {
		t.Errorf("reached %d of an expected %d, want the shortfall to be visible", reached, want)
	}
}

// A refused token is the failure worth separating, and it no longer carries a replica label: a dial
// that was rejected belongs to no replica, because the gateway's answer is what names one.
func TestARefusedTokenIsCountedAsUnauthorized(t *testing.T) {
	server := &blockingServer{}
	gw := startGateway(t, &gateway{reject: tunnel.ReasonUnauthorized})

	metrics := NewMetrics()
	supervisor, stop := runSupervisor(t, gw.addr, server, metrics)
	defer stop()

	eventually(t, "the refusal to be counted as unauthorized", func() bool {
		return counterValue(t, metrics, "hangar_agent_dial_failures_total", map[string]string{
			"reason": reasonUnauthorized,
		}) > 0
	})
	if err := supervisor.Ready(); err == nil {
		t.Error("Ready returned nil with every dial refused")
	}
}

// Scaling the gateway down has to retire the workers chasing a replica that is gone, and forget it:
// a gauge left at zero for something that no longer exists alerts forever.
func TestSupervisorFollowsTheGatewayShrinking(t *testing.T) {
	server := &blockingServer{}
	gw := startGateway(t, &gateway{
		ids:      []string{"hangar-gateway-0", "hangar-gateway-1"},
		accepted: func(net.Conn) {},
	})

	metrics := NewMetrics()
	supervisor, stop := runSupervisor(t, gw.addr, server, metrics)
	defer stop()

	eventually(t, "both replicas covered", func() bool { return len(supervisor.Coverage()) == 2 })

	// The manifest edit and the rollout it causes: the gateway reports one replica from here, and
	// every tunnel is severed, which is what makes the agent redial and read the new number.
	gw.scale([]string{"hangar-gateway-0"})
	_ = server.sever()

	eventually(t, "the departed replica to be forgotten", func() bool {
		coverage := supervisor.Coverage()
		return len(coverage) == 1 && coverage["hangar-gateway-0"] == 1 && supervisor.Expected() == 1
	})
	if _, err := seriesFor(metrics, "hangar_agent_tunnels", map[string]string{
		"gateway": "hangar-gateway-1",
	}); err == nil {
		t.Error("the gauge for the departed replica is still in the registry")
	}
}

// Shutdown is a rollout of the agent, which is an ordinary event: the streams already accepted
// are somebody's half-written completion and have to finish.
func TestSupervisorDrainsInFlightRequests(t *testing.T) {
	release := make(chan struct{})
	served := make(chan struct{})
	server, err := tunnel.NewConnector(tunnel.ConnectorConfig{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			close(served)
			<-release
			_, _ = w.Write([]byte("finished"))
		}),
	})
	if err != nil {
		t.Fatalf("tunnel.NewConnector: %v", err)
	}

	pool, err := tunnel.NewPool(tunnel.PoolConfig{})
	if err != nil {
		t.Fatalf("tunnel.NewPool: %v", err)
	}
	defer func() { _ = pool.Close() }()

	const clusterID = "clu_drain"
	tr := startGateway(t, &gateway{accepted: func(conn net.Conn) {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := pool.Adopt(ctx, clusterID, conn); err != nil {
			t.Errorf("Adopt: %v", err)
		}
	}})

	supervisor, err := NewSupervisor(SupervisorConfig{
		Gateway:         tr.addr,
		ConnsPerGateway: 1,
		Dial:            testDial(t, tr.addr),
		Server:          server,
		BackoffMin:      time.Millisecond,
		BackoffMax:      5 * time.Millisecond,
		Log:             quietLogger(),
	})
	if err != nil {
		t.Fatalf("NewSupervisor: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	ran := make(chan struct{})
	go func() {
		defer close(ran)
		supervisor.Run(ctx)
	}()

	eventually(t, "a tunnel", func() bool { return pool.Live(clusterID) > 0 })

	client := &http.Client{Transport: pool.RoundTripper(clusterID)}
	type result struct {
		resp *http.Response
		err  error
	}
	results := make(chan result, 1)
	go func() {
		resp, err := client.Get(tunnel.Authority(clusterID).String() + "/v1/models")
		results <- result{resp, err}
	}()
	<-served

	cancel()
	drain, cancelDrain := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelDrain()
	drained := make(chan error, 1)
	go func() { drained <- server.Shutdown(drain) }()

	close(release)
	got := <-results
	if got.err != nil {
		t.Fatalf("the in-flight request failed instead of draining: %v", got.err)
	}
	body, _ := io.ReadAll(got.resp.Body)
	_ = got.resp.Body.Close()
	if string(body) != "finished" {
		t.Errorf("body = %q, want finished", body)
	}
	if err := <-drained; err != nil {
		t.Errorf("Shutdown: %v", err)
	}

	select {
	case <-ran:
	case <-time.After(5 * time.Second):
		t.Fatal("the supervisor never let go")
	}
	if supervisor.Tunnels() != 0 {
		t.Errorf("tunnels after drain = %d, want 0", supervisor.Tunnels())
	}
}

func TestNewSupervisorRefusesAnUnusableConfig(t *testing.T) {
	nothing := func(context.Context, string, []string) (net.Conn, Peer, error) { return nil, Peer{}, nil }
	base := SupervisorConfig{Gateway: "gw:9443", Dial: nothing, Server: &blockingServer{}}

	noGateway := base
	noGateway.Gateway = ""
	if _, err := NewSupervisor(noGateway); err == nil {
		t.Error("NewSupervisor accepted no gateway address")
	}
	noDial := base
	noDial.Dial = nil
	if _, err := NewSupervisor(noDial); err == nil {
		t.Error("NewSupervisor accepted no dialler")
	}
	noServer := base
	noServer.Server = nil
	if _, err := NewSupervisor(noServer); err == nil {
		t.Error("NewSupervisor accepted no server")
	}
}

func TestBackoffIsBounded(t *testing.T) {
	s, err := NewSupervisor(SupervisorConfig{
		Gateway:    "gw:9443",
		Dial:       func(context.Context, string, []string) (net.Conn, Peer, error) { return nil, Peer{}, nil },
		Server:     &blockingServer{},
		BackoffMin: 100 * time.Millisecond,
		BackoffMax: time.Second,
		Log:        quietLogger(),
	})
	if err != nil {
		t.Fatalf("NewSupervisor: %v", err)
	}
	if got := s.backoff(0); got != 0 {
		t.Errorf("backoff(0) = %s, want 0 so the first dial is immediate", got)
	}
	for attempt := 1; attempt < 20; attempt++ {
		if got := s.backoff(attempt); got < 0 || got > time.Second {
			t.Fatalf("backoff(%d) = %s, outside 0..1s", attempt, got)
		}
	}
}

// runSupervisor starts one with a real dialler at target and returns a stop function.
func runSupervisor(t *testing.T, target string, server Server, metrics *Metrics) (*Supervisor, func()) {
	t.Helper()
	supervisor, err := NewSupervisor(SupervisorConfig{
		Gateway:         target,
		ConnsPerGateway: 1,
		Dial:            testDial(t, target),
		Server:          server,
		BackoffMin:      time.Millisecond,
		BackoffMax:      5 * time.Millisecond,
		Metrics:         metrics,
		Log:             quietLogger(),
	})
	if err != nil {
		t.Fatalf("NewSupervisor: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		supervisor.Run(ctx)
	}()
	return supervisor, func() {
		// Cancelling stops dialling and nothing else: a tunnel the far end is still holding open
		// keeps Serve blocked, which is exactly the behaviour drain relies on. Severing is the
		// server's own call, so a test that is finished has to make it.
		cancel()
		if closer, ok := server.(io.Closer); ok {
			_ = closer.Close()
		}
		<-done
	}
}

func testDial(t *testing.T, target string) DialFunc {
	t.Helper()
	cfg := dialConfig(target)
	cfg.TLSEnabled = false
	dialler, err := NewDialler(cfg)
	if err != nil {
		t.Fatalf("NewDialler: %v", err)
	}
	return dialler.Dial
}

// blockingServer stands in for the HTTP/2 server: it holds every connection until the far end closes
// or Close severs it, which is the shape the supervisor sees from tunnel.Connector.
type blockingServer struct {
	mu     sync.Mutex
	conns  []net.Conn
	closed bool
}

func (s *blockingServer) Serve(conn net.Conn) error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		_ = conn.Close()
		return tunnel.ErrConnectorClosed
	}
	s.conns = append(s.conns, conn)
	s.mu.Unlock()

	buf := make([]byte, 1)
	for {
		if _, err := conn.Read(buf); err != nil {
			_ = conn.Close()
			return err
		}
	}
}

func (s *blockingServer) Close() error {
	s.mu.Lock()
	s.closed = true
	s.mu.Unlock()
	return s.sever()
}

// sever drops every tunnel without shutting down, which is what a rollout of the gateway looks like
// from here: the connections go, and the agent is expected to come back.
func (s *blockingServer) sever() error {
	s.mu.Lock()
	conns := s.conns
	s.conns = nil
	s.mu.Unlock()
	for _, conn := range conns {
		_ = conn.Close()
	}
	return nil
}
