package tunnel

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

// stubVerifier is the plane, as the listener sees it.
//
// `clusters` maps a token to the uid and `names` to the operator-facing name, the
// two apart because the plane answers with both and an agent may claim either.
// A token absent from `names` resolves to an identity with no name, which is what
// a plane too old to send one looks like.
type stubVerifier struct {
	mu       sync.Mutex
	clusters map[string]string
	names    map[string]string
	err      error
	calls    int
}

func (v *stubVerifier) VerifyTunnelToken(_ context.Context, token string) (Identity, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.calls++
	if v.err != nil {
		return Identity{}, v.err
	}
	cluster, ok := v.clusters[token]
	if !ok {
		return Identity{}, fmt.Errorf("%w: no such cluster", ErrRejected)
	}
	return Identity{ClusterID: cluster, ClusterName: v.names[token]}, nil
}

func (v *stubVerifier) fail(err error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.err = err
}

func (v *stubVerifier) asked() int {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.calls
}

// dialLog records why dials were refused.
type dialLog struct {
	mu      sync.Mutex
	reasons []string
}

func (d *dialLog) TunnelDialRejected(reason string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.reasons = append(d.reasons, reason)
}

func (d *dialLog) last() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(d.reasons) == 0 {
		return ""
	}
	return d.reasons[len(d.reasons)-1]
}

// startListener brings up a tunnel listener on a loopback port.
func startListener(t *testing.T, cfg ListenerConfig) (*Pool, *Listener, string) {
	t.Helper()
	if cfg.Pool == nil {
		cfg.Pool = newPool(t, PoolConfig{})
	}
	listener, err := NewListener(cfg)
	if err != nil {
		t.Fatalf("new listener: %v", err)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	served := make(chan error, 1)
	go func() { served <- listener.Serve(ctx, ln) }()
	t.Cleanup(func() {
		cancel()
		_ = listener.Close()
		select {
		case err := <-served:
			if err != nil {
				t.Errorf("serve: %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Error("the accept loop did not return")
		}
	})
	return cfg.Pool, listener, ln.Addr().String()
}

// dialConnector dials the listener and, when it is let in, serves HTTP/2 back
// down the connection exactly as a real connector does.
func dialConnector(t *testing.T, addr string, hello Hello, handler http.Handler) (Accept, error) {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	accept, err := Handshake(ctx, conn, hello)
	if err != nil {
		_ = conn.Close()
		return accept, err
	}

	connector := newConnector(t, handler, Limits{})
	go func() { _ = connector.Serve(conn) }()
	return accept, nil
}

func echoHandler(body string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(body))
	})
}

// The handshake is where an agent learns what to hold tunnels to, and it has to be the handshake:
// a cluster that had to ask the plane how many gateway replicas there are could not recover its own
// data path through a control-plane outage.
func TestAnAcceptedDialIsToldWhichReplicaAndHowMany(t *testing.T) {
	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
	_, _, addr := startListener(t, ListenerConfig{
		Verifier:  verifier,
		GatewayID: "hangar-gateway-1",
		Gateways:  3,
	})

	accept, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("ok"))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	if accept.GatewayID != "hangar-gateway-1" {
		t.Errorf("GatewayID = %q, want the replica that answered", accept.GatewayID)
	}
	if accept.Gateways != 3 {
		t.Errorf("Gateways = %d, want 3", accept.Gateways)
	}
}

// The shape every chart-installed agent actually dials with, and the one that was refused in
// production while every test passed. The chart writes `cluster.id` from what the plane rendered,
// which is the cluster's name — so the agent claims the name while the plane answers with the uid,
// and a listener comparing the claim against the uid alone rejects the whole fleet. The e2e missed
// it by dialling with the uid, which no agent has.
func TestAnAgentClaimingTheClusterNameIsAcceptedAndKeyedOnTheUid(t *testing.T) {
	verifier := &stubVerifier{
		clusters: map[string]string{"tok-ap": "clu_ap"},
		names:    map[string]string{"tok-ap": "cluster-alpha"},
	}
	pool, _, addr := startListener(t, ListenerConfig{Verifier: verifier})

	accept, err := dialConnector(t, addr,
		Hello{Token: "tok-ap", ClusterID: "cluster-alpha"}, echoHandler("pong"))
	if err != nil {
		t.Fatalf("an agent claiming the cluster's name was refused: %v", err)
	}
	if !accept.OK {
		t.Fatalf("accept = %+v", accept)
	}
	// The uid comes back, not the name that was claimed: the agent's own routing
	// and the pool have to agree, and the uid is the half that cannot be renamed.
	if accept.ClusterID != "clu_ap" {
		t.Fatalf("ClusterID = %q, want the uid", accept.ClusterID)
	}
	eventually(t, 5*time.Second, "the tunnel keyed on the uid", func() bool { return pool.Live("clu_ap") == 1 })
	if got := pool.Live("cluster-alpha"); got != 0 {
		t.Fatalf("the pool holds %d tunnels under the name; it must key on the uid alone", got)
	}
}

// A gateway that was never told its replica count must say one rather than none. Zero on the wire
// would reach an agent as "hold no tunnels", which is the one answer that is never right.
func TestAnUnconfiguredReplicaCountIsReportedAsOne(t *testing.T) {
	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
	_, _, addr := startListener(t, ListenerConfig{Verifier: verifier})

	accept, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("ok"))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	if accept.Gateways != 1 {
		t.Errorf("Gateways = %d, want 1", accept.Gateways)
	}
}

func TestADialThatAuthenticatesBecomesATunnelAndCarriesARequest(t *testing.T) {
	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
	pool, _, addr := startListener(t, ListenerConfig{Verifier: verifier})

	accept, err := dialConnector(t, addr, Hello{Token: "tok-ap", ClusterID: "clu_ap"}, echoHandler("pong"))
	if err != nil {
		t.Fatalf("handshake: %v", err)
	}
	if !accept.OK || accept.ClusterID != "clu_ap" {
		t.Fatalf("accept = %+v", accept)
	}
	eventually(t, 5*time.Second, "the tunnel to be live", func() bool { return pool.Live("clu_ap") == 1 })

	req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := pool.RoundTripper("clu_ap").RoundTrip(req)
	if err != nil {
		t.Fatalf("round trip: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, _ := io.ReadAll(resp.Body)
	if string(body) != "pong" {
		t.Fatalf("body = %q", body)
	}
}

func TestDialsThatDoNotAuthenticateAreRefusedWithAReason(t *testing.T) {
	cases := []struct {
		name    string
		hello   Hello
		garbage string
		planeUp bool
		reason  string
	}{
		{name: "unknown token", hello: Hello{Token: "nope"}, planeUp: true, reason: ReasonUnauthorized},
		{name: "token for another cluster", hello: Hello{Token: "tok-ap", ClusterID: "clu_us"}, planeUp: true, reason: ReasonUnauthorized},
		// Accepting the name as well as the uid must not have widened this to any
		// string: another cluster's *name* is exactly the copy-pasted Secret the
		// check exists for.
		{name: "another cluster's name", hello: Hello{Token: "tok-ap", ClusterID: "inference-us"}, planeUp: true, reason: ReasonUnauthorized},
		{name: "plane unreachable", hello: Hello{Token: "tok-ap"}, reason: ReasonUnavailable},
		{name: "not a hangar dial", garbage: "GET / HTTP/1.1\r\n\r\n", planeUp: true, reason: ReasonHandshake},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
			if !tc.planeUp {
				verifier.fail(errors.New("dial tcp: connection refused"))
			}
			dials := &dialLog{}
			pool, _, addr := startListener(t, ListenerConfig{Verifier: verifier, Dials: dials})

			if tc.garbage != "" {
				conn, err := net.Dial("tcp", addr)
				if err != nil {
					t.Fatalf("dial: %v", err)
				}
				_, _ = conn.Write([]byte(tc.garbage))
				_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
				_, _ = io.ReadAll(conn)
				_ = conn.Close()
			} else if _, err := dialConnector(t, addr, tc.hello, echoHandler("pong")); err == nil {
				t.Fatal("expected the dial to be refused")
			}

			eventually(t, 5*time.Second, "the dial to be recorded", func() bool { return dials.last() != "" })
			if got := dials.last(); got != tc.reason {
				t.Fatalf("reason = %q, want %q", got, tc.reason)
			}
			if got := len(pool.Clusters()); got != 0 {
				t.Fatalf("clusters = %d, want no tunnel from a refused dial", got)
			}
		})
	}
}

// TestAPlaneOutageOnlyAffectsNewDialsAndNotEvenAllOfThose is the property the
// whole verification path exists to keep. A plane that cannot be reached never
// closes a tunnel that is already up, and a connector that redials during an
// outage is let back in on the answer the listener already has.
func TestAPlaneOutageOnlyAffectsNewDialsAndNotEvenAllOfThose(t *testing.T) {
	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
	// A TTL short enough that the second dial has to ask the plane again, and a
	// grace long enough that the answer it cannot get is still remembered.
	pool, _, addr := startListener(t, ListenerConfig{
		Verifier:   verifier,
		CacheTTL:   time.Nanosecond,
		CacheGrace: time.Hour,
	})

	if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("first")); err != nil {
		t.Fatalf("first handshake: %v", err)
	}
	eventually(t, 5*time.Second, "the first tunnel", func() bool { return pool.Live("clu_ap") == 1 })

	verifier.fail(errors.New("dial tcp: connection refused"))

	if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("second")); err != nil {
		t.Fatalf("a redial during a plane outage must be let in from cache: %v", err)
	}
	eventually(t, 5*time.Second, "the second tunnel", func() bool { return pool.Live("clu_ap") == 2 })
	if verifier.asked() < 2 {
		t.Fatalf("the plane was asked %d times, want the second dial to have tried it", verifier.asked())
	}

	// And a token nobody has ever verified is still refused, so the cache is a
	// memory and not a bypass.
	if _, err := dialConnector(t, addr, Hello{Token: "tok-us"}, echoHandler("third")); err == nil {
		t.Fatal("an unknown token must not be admitted during a plane outage")
	}
}

// TestAFreshlyVerifiedTokenIsNotReVerified keeps a connector crash-loop from
// turning into a load test of the control plane.
func TestAFreshlyVerifiedTokenIsNotReVerified(t *testing.T) {
	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
	pool, _, addr := startListener(t, ListenerConfig{Verifier: verifier})

	for i := 0; i < 3; i++ {
		if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong")); err != nil {
			t.Fatalf("handshake %d: %v", i, err)
		}
	}
	eventually(t, 5*time.Second, "three tunnels", func() bool { return pool.Live("clu_ap") == 3 })
	if verifier.asked() != 1 {
		t.Fatalf("the plane was asked %d times, want once", verifier.asked())
	}
}

func TestShutdownStopsAcceptingAndDrainsTheTunnelsAlreadyHeld(t *testing.T) {
	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
	pool, listener, addr := startListener(t, ListenerConfig{Verifier: verifier})

	if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong")); err != nil {
		t.Fatalf("handshake: %v", err)
	}
	eventually(t, 5*time.Second, "the tunnel to be live", func() bool { return pool.Live("clu_ap") == 1 })

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := listener.Shutdown(ctx); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	if got := pool.Live("clu_ap"); got != 0 {
		t.Fatalf("live = %d after the drain", got)
	}
	if conn, err := net.Dial("tcp", addr); err == nil {
		_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		if _, err := io.ReadAll(conn); err == nil {
			t.Fatal("the listener is still accepting after shutdown")
		}
		_ = conn.Close()
	}
}
