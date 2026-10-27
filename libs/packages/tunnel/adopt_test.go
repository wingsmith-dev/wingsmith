package tunnel

import (
	"context"
	"errors"
	"net"
	"sync"
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

// gaugeObserver keeps the last count it was given per cluster, the way a
// Prometheus gauge that is Set on every report does.
type gaugeObserver struct {
	mu   sync.Mutex
	last map[string]int
}

func (o *gaugeObserver) set(clusterID string, live int) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.last[clusterID] = live
}

func (o *gaugeObserver) get(clusterID string) int {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.last[clusterID]
}

func (o *gaugeObserver) TunnelOpened(clusterID string, live int) { o.set(clusterID, live) }
func (o *gaugeObserver) TunnelClosed(clusterID string, live int) { o.set(clusterID, live) }

// TestTheLastCoverageReportCountsEveryTunnel adopts tunnels to one cluster at
// the same moment. Each report used to carry a count taken before it was sent,
// so two of them could arrive in the opposite order and leave the gauge at one
// less than the pool held until the next open or close.
func TestTheLastCoverageReportCountsEveryTunnel(t *testing.T) {
	t.Parallel()

	const tunnels = 8
	for round := range 20 {
		observer := &gaugeObserver{last: make(map[string]int)}
		pool := newPool(t, PoolConfig{Observer: observer})

		// The sockets and connectors are set up first, on the test goroutine,
		// so the adopts are what runs concurrently.
		gatewaySides := make([]net.Conn, tunnels)
		for i := range tunnels {
			gatewaySide, connectorSide := socketPair(t)
			connector := newConnector(t, echoHandler("a"), Limits{})
			go func() { _ = connector.Serve(connectorSide) }()
			gatewaySides[i] = gatewaySide
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		var wg sync.WaitGroup
		for _, gatewaySide := range gatewaySides {
			wg.Add(1)
			go func() {
				defer wg.Done()
				if _, err := pool.Adopt(ctx, "cluster-a", gatewaySide); err != nil {
					t.Errorf("adopt: %v", err)
				}
			}()
		}
		wg.Wait()
		cancel()

		if live := pool.Live("cluster-a"); live != tunnels {
			t.Fatalf("round %d: the pool holds %d live tunnels, want %d", round, live, tunnels)
		}
		if got := observer.get("cluster-a"); got != tunnels {
			t.Fatalf("round %d: the last report said %d tunnels, the pool holds %d", round, got, tunnels)
		}
	}
}

// TestStreamUseIsReportedPerClusterOverItsLiveTunnels pins what the gateway's
// stream gauges read: the requests open and the limits advertised, summed over
// the tunnels that can still take a request.
func TestStreamUseIsReportedPerClusterOverItsLiveTunnels(t *testing.T) {
	t.Parallel()

	pool := newPool(t, PoolConfig{})
	busy := link(t, pool, "cluster-a", newConnector(t, echoHandler("a"), Limits{MaxConcurrentStreams: 4}))
	link(t, pool, "cluster-a", newConnector(t, echoHandler("a"), Limits{MaxConcurrentStreams: 6}))
	closing := link(t, pool, "cluster-b", newConnector(t, echoHandler("b"), Limits{MaxConcurrentStreams: 8}))
	link(t, pool, "cluster-b", newConnector(t, echoHandler("b"), Limits{MaxConcurrentStreams: 2}))
	occupy(busy, 3)
	_ = closing.Close()

	eventually(t, 5*time.Second, "the closed tunnel to leave the report", func() bool {
		got := pool.Streams()
		return len(got) == 2 && got[1].Tunnels == 1
	})
	want := []ClusterStreams{
		{ClusterID: "cluster-a", Tunnels: 2, Active: 3, Capacity: 10},
		{ClusterID: "cluster-b", Tunnels: 1, Active: 0, Capacity: 2},
	}
	got := pool.Streams()
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("Streams()[%d] = %+v, want %+v", i, got[i], want[i])
		}
	}
}
