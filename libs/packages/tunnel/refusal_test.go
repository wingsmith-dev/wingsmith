package tunnel

import (
	"errors"
	"sync/atomic"
	"testing"
	"time"
)

// countingDials is a DialObserver that also counts duplicates.
type countingDials struct {
	dialLog
	duplicates atomic.Int32
}

func (d *countingDials) TunnelDialDuplicate() { d.duplicates.Add(1) }

// TestOnlyARefusedCredentialReadsAsRejected pins what an agent is told. A plane
// the gateway cannot reach, or a gateway at its tunnel limit, is not the
// agent's credential being wrong, and reporting it as ErrRejected sent people
// looking at Secrets during a control-plane outage.
func TestOnlyARefusedCredentialReadsAsRejected(t *testing.T) {
	t.Parallel()

	t.Run("unauthorized", func(t *testing.T) {
		t.Parallel()
		verifier := &stubVerifier{clusters: map[string]string{}}
		_, _, addr := startListener(t, ListenerConfig{Verifier: verifier})
		_, err := dialConnector(t, addr, Hello{Token: "nope"}, echoHandler("pong"))
		if !errors.Is(err, ErrRejected) {
			t.Fatalf("an unknown token returned %v, want ErrRejected", err)
		}
	})

	t.Run("plane unreachable", func(t *testing.T) {
		t.Parallel()
		verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
		verifier.fail(errors.New("dial tcp: connection refused"))
		_, _, addr := startListener(t, ListenerConfig{Verifier: verifier})
		_, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong"))
		if !errors.Is(err, ErrRefused) || errors.Is(err, ErrRejected) {
			t.Fatalf("an unreachable plane returned %v, want ErrRefused and not ErrRejected", err)
		}
	})

	t.Run("at the tunnel limit", func(t *testing.T) {
		t.Parallel()
		verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
		dials := &dialLog{}
		pool, _, addr := startListener(t, ListenerConfig{
			Verifier: verifier,
			Dials:    dials,
			Pool:     newPool(t, PoolConfig{MaxTunnelsPerCluster: 1}),
		})
		if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong")); err != nil {
			t.Fatalf("first dial: %v", err)
		}
		eventually(t, 5*time.Second, "the first tunnel", func() bool { return pool.Live("clu_ap") == 1 })
		_, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong"))
		if !errors.Is(err, ErrRefused) || errors.Is(err, ErrRejected) {
			t.Fatalf("a dial past the limit returned %v, want ErrRefused", err)
		}
		eventually(t, 5*time.Second, "the refusal to be recorded", func() bool { return dials.last() == ReasonLimit })
	})
}

// TestADialToACoveredReplicaIsAnsweredNotAdopted pins the duplicate path. The
// agent says which replicas it already holds its quota on, so a dial the load
// balancer sent back to one of them is answered with the replica count and
// closed, rather than adopted and then dropped by the agent — which the gateway
// used to count as a dial that failed to authenticate.
func TestADialToACoveredReplicaIsAnsweredNotAdopted(t *testing.T) {
	t.Parallel()

	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
	dials := &countingDials{}
	pool, _, addr := startListener(t, ListenerConfig{
		Verifier:  verifier,
		Dials:     dials,
		GatewayID: "gateway-0",
		Gateways:  3,
	})

	accept, err := dialConnector(t, addr, Hello{Token: "tok-ap", Covered: []string{"gateway-0"}}, echoHandler("pong"))
	if !errors.Is(err, ErrDuplicate) {
		t.Fatalf("a dial to a covered replica returned %v, want ErrDuplicate", err)
	}
	if accept.GatewayID != "gateway-0" || accept.Gateways != 3 {
		t.Errorf("the answer named replica %q of %d, want gateway-0 of 3", accept.GatewayID, accept.Gateways)
	}
	eventually(t, 5*time.Second, "the duplicate to be counted", func() bool { return dials.duplicates.Load() == 1 })
	if got := dials.last(); got != "" {
		t.Errorf("a duplicate was counted as a refusal with reason %q", got)
	}
	if live := pool.Live("clu_ap"); live != 0 {
		t.Errorf("a duplicate dial became %d tunnels", live)
	}

	// A replica the agent does not list is accepted as usual.
	if _, err := dialConnector(t, addr, Hello{Token: "tok-ap", Covered: []string{"gateway-1"}}, echoHandler("pong")); err != nil {
		t.Fatalf("a dial to an uncovered replica: %v", err)
	}
}
