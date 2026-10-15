package tunnel

import (
	"context"
	"errors"
	"testing"
	"time"
)

// TestOneClusterCannotHoldMoreTunnelsThanItsCap pins the per-cluster and the
// pool-wide caps. A leaked token, or an agent stuck redialling, would otherwise
// grow the gateway by two goroutines and a receive window per dial.
func TestOneClusterCannotHoldMoreTunnelsThanItsCap(t *testing.T) {
	t.Parallel()

	t.Run("per cluster", func(t *testing.T) {
		t.Parallel()
		pool := newPool(t, PoolConfig{MaxTunnelsPerCluster: 2})
		link(t, pool, "cluster-a", newConnector(t, echoHandler("a"), Limits{}))
		link(t, pool, "cluster-a", newConnector(t, echoHandler("a"), Limits{}))

		if err := pool.Admit("cluster-a"); !errors.Is(err, ErrTunnelLimit) {
			t.Errorf("Admit at the cap = %v, want ErrTunnelLimit", err)
		}
		gatewaySide, connectorSide := socketPair(t)
		go func() { _ = newConnector(t, echoHandler("a"), Limits{}).Serve(connectorSide) }()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := pool.Adopt(ctx, "cluster-a", gatewaySide); !errors.Is(err, ErrTunnelLimit) {
			t.Errorf("third tunnel adopted with %v, want ErrTunnelLimit", err)
		}
		if live := pool.Live("cluster-a"); live != 2 {
			t.Errorf("cluster-a has %d live tunnels, want 2", live)
		}
		// Another cluster is not affected by this one's cap.
		link(t, pool, "cluster-b", newConnector(t, echoHandler("b"), Limits{}))
	})

	t.Run("per replica", func(t *testing.T) {
		t.Parallel()
		pool := newPool(t, PoolConfig{MaxTunnels: 1})
		link(t, pool, "cluster-a", newConnector(t, echoHandler("a"), Limits{}))
		if err := pool.Admit("cluster-b"); !errors.Is(err, ErrTunnelLimit) {
			t.Errorf("Admit past the pool cap = %v, want ErrTunnelLimit", err)
		}
	})
}

// TestATunnelAdoptedDuringShutdownIsClosedNotLeaked covers the window between
// Adopt's first look at the pool and the moment it adds the tunnel. Shutdown
// collects the tunnels it drains once, so a tunnel added after that would be
// held open by nothing and drained by nobody.
func TestATunnelAdoptedDuringShutdownIsClosedNotLeaked(t *testing.T) {
	t.Parallel()

	pool := newPool(t, PoolConfig{})
	gatewaySide, connectorSide := socketPair(t)
	stalling := &stallingConn{Conn: gatewaySide}
	go func() { _ = newConnector(t, echoHandler("a"), Limits{}).Serve(connectorSide) }()

	// Hold the HTTP/2 preface so Adopt is parked past its first check.
	stalling.stall()
	t.Cleanup(stalling.release)
	type result struct {
		conn *Conn
		err  error
	}
	adopted := make(chan result, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		conn, err := pool.Adopt(ctx, "cluster-a", stalling)
		adopted <- result{conn, err}
	}()

	time.Sleep(50 * time.Millisecond)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := pool.Shutdown(ctx); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	stalling.release()

	select {
	case got := <-adopted:
		if !errors.Is(got.err, ErrPoolClosed) {
			t.Fatalf("Adopt after Shutdown returned %v, want ErrPoolClosed", got.err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Adopt never returned")
	}
	if live := pool.Live("cluster-a"); live != 0 {
		t.Errorf("the pool holds %d tunnels after Shutdown, want 0", live)
	}
}
