package tunnel

import (
	"errors"
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
