package tunnel

import (
	"context"
	"errors"
	"net"
	"sync/atomic"
	"testing"
	"time"
)

// revocationLog counts tunnels closed for their token.
type revocationLog struct {
	dialLog
	revoked atomic.Int32
}

func (r *revocationLog) TunnelRevoked(string) { r.revoked.Add(1) }

func (v *stubVerifier) revoke(token string) {
	v.mu.Lock()
	defer v.mu.Unlock()
	delete(v.clusters, token)
}

func (v *stubVerifier) move(token, cluster string) {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.clusters[token] = cluster
}

// TestARevokedTokenLosesItsOpenTunnels is the leaked-token case. Tokens used to
// be checked once, on the dial, so revoking one in the plane left every tunnel
// it had opened carrying that cluster's traffic until the agent happened to
// redial.
func TestARevokedTokenLosesItsOpenTunnels(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct {
		name   string
		change func(*stubVerifier)
		closed bool
	}{
		{"the plane refuses the token", func(v *stubVerifier) { v.revoke("tok-ap") }, true},
		{"the token now names another cluster", func(v *stubVerifier) { v.move("tok-ap", "clu_us") }, true},
		{"the plane cannot be reached", func(v *stubVerifier) { v.fail(errors.New("connection refused")) }, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
			dials := &revocationLog{}
			pool, _, addr := startListener(t, ListenerConfig{
				Verifier:         verifier,
				Dials:            dials,
				CacheTTL:         time.Nanosecond,
				ReverifyInterval: 20 * time.Millisecond,
			})
			for i := 0; i < 2; i++ {
				if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong")); err != nil {
					t.Fatalf("dial %d: %v", i, err)
				}
			}
			eventually(t, 5*time.Second, "two tunnels", func() bool { return pool.Live("clu_ap") == 2 })

			tc.change(verifier)
			if tc.closed {
				eventually(t, 5*time.Second, "the tunnels to close", func() bool { return pool.Live("clu_ap") == 0 })
				if got := dials.revoked.Load(); got != 2 {
					t.Errorf("%d revocations observed, want 2", got)
				}
				return
			}
			before := verifier.asked()
			eventually(t, 5*time.Second, "a few re-verifications", func() bool { return verifier.asked() >= before+4 })
			if live := pool.Live("clu_ap"); live != 2 {
				t.Errorf("an unreachable plane closed tunnels: %d live, want 2", live)
			}
		})
	}
}

// TestRevokeClosesAClustersTunnelsAndForgetsItsTokens is the explicit path.
func TestRevokeClosesAClustersTunnelsAndForgetsItsTokens(t *testing.T) {
	t.Parallel()

	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap", "tok-us": "clu_us"}}
	pool, listener, addr := startListener(t, ListenerConfig{Verifier: verifier})
	for _, token := range []string{"tok-ap", "tok-ap", "tok-us"} {
		if _, err := dialConnector(t, addr, Hello{Token: token}, echoHandler("pong")); err != nil {
			t.Fatalf("dial: %v", err)
		}
	}
	eventually(t, 5*time.Second, "three tunnels", func() bool {
		return pool.Live("clu_ap") == 2 && pool.Live("clu_us") == 1
	})
	asked := verifier.asked()

	if got := listener.Revoke("clu_ap"); got != 2 {
		t.Errorf("Revoke closed %d tunnels, want 2", got)
	}
	eventually(t, 5*time.Second, "clu_ap's tunnels to close", func() bool { return pool.Live("clu_ap") == 0 })
	if live := pool.Live("clu_us"); live != 1 {
		t.Errorf("revoking clu_ap touched clu_us: %d live", live)
	}

	verifier.revoke("tok-ap")
	if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong")); !errors.Is(err, ErrRejected) {
		t.Fatalf("a revoked cluster's token was let back in from cache: %v", err)
	}
	if verifier.asked() != asked+1 {
		t.Errorf("the plane was asked %d more times, want once", verifier.asked()-asked)
	}
}

// breakableListener starts failing Accept with an error that is not temporary
// once broken, the way a listener whose socket has gone bad does.
type breakableListener struct {
	net.Listener
	broken atomic.Bool
}

func (l *breakableListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if l.broken.Load() {
		if conn != nil {
			_ = conn.Close()
		}
		return nil, errors.New("accept: the socket is gone")
	}
	return conn, err
}

// TestTunnelsOutliveAServeThatReturnedAndAreStillReverified pins the contract
// the gateway relies on to bind the listener again instead of exiting: a Serve
// that returns on an accept error must not stop the checks on tunnels it
// already adopted, or a revoked token would keep them.
func TestTunnelsOutliveAServeThatReturnedAndAreStillReverified(t *testing.T) {
	t.Parallel()

	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
	pool := newPool(t, PoolConfig{})
	listener, err := NewListener(ListenerConfig{
		Pool:             pool,
		Verifier:         verifier,
		CacheTTL:         time.Nanosecond,
		ReverifyInterval: 20 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	inner, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	breakable := &breakableListener{Listener: inner}
	served := make(chan error, 1)
	go func() { served <- listener.Serve(context.Background(), breakable) }()

	addr := inner.Addr().String()
	if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong")); err != nil {
		t.Fatalf("dial: %v", err)
	}
	eventually(t, 5*time.Second, "the tunnel", func() bool { return pool.Live("clu_ap") == 1 })

	breakable.broken.Store(true)
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.Close()
	select {
	case err := <-served:
		if err == nil {
			t.Fatal("Serve returned nil on an accept error that is not temporary")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Serve did not return on an accept error that is not temporary")
	}
	if live := pool.Live("clu_ap"); live != 1 {
		t.Fatalf("Serve returning closed tunnels: %d live, want 1", live)
	}

	verifier.revoke("tok-ap")
	eventually(t, 5*time.Second, "the revoked tunnel to close", func() bool { return pool.Live("clu_ap") == 0 })
}
