package tunnel

import (
	"context"
	"net"
	"net/http"
	"sync"
	"testing"
	"time"
)

// stallingConn is a gateway-side socket whose writes can be held, which is what
// a peer that has stopped reading looks like once the kernel buffers are full.
type stallingConn struct {
	net.Conn
	mu      sync.Mutex
	stalled chan struct{}
}

func (c *stallingConn) stall() {
	c.mu.Lock()
	c.stalled = make(chan struct{})
	c.mu.Unlock()
}

func (c *stallingConn) release() {
	c.mu.Lock()
	if c.stalled != nil {
		close(c.stalled)
		c.stalled = nil
	}
	c.mu.Unlock()
}

func (c *stallingConn) Write(p []byte) (int, error) {
	c.mu.Lock()
	gate := c.stalled
	c.mu.Unlock()
	if gate != nil {
		<-gate
	}
	return c.Conn.Write(p)
}

// TestAStalledTunnelDoesNotBlockAnotherCluster pins that the pool never asks a
// tunnel for its state while holding the pool-wide lock. ClientConn.State waits
// on the connection's write lock, which a write to a peer that stopped reading
// holds for up to WriteByteTimeout, so asking under the pool lock let one
// stalled tunnel freeze routing to every cluster.
func TestAStalledTunnelDoesNotBlockAnotherCluster(t *testing.T) {
	t.Parallel()

	pool := newPool(t, PoolConfig{})

	stalledSide, connectorSide := socketPair(t)
	stalling := &stallingConn{Conn: stalledSide}
	connectorA := newConnector(t, echoHandler("a"), Limits{})
	go func() { _ = connectorA.Serve(connectorSide) }()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := pool.Adopt(ctx, "cluster-a", stalling); err != nil {
		t.Fatalf("adopt: %v", err)
	}
	t.Cleanup(stalling.release)

	link(t, pool, "cluster-b", newConnector(t, echoHandler("b"), Limits{}))

	stalling.stall()
	go func() {
		req := gatewayShapedRequest(t, "/v1/chat/completions", `{"model":"m"}`)
		resp, err := pool.RoundTripper("cluster-a").RoundTrip(req)
		if err == nil {
			_ = resp.Body.Close()
		}
	}()
	// Give the request time to take cluster-a's write lock, then ask about
	// cluster-a from another goroutine: before the fix that held the pool lock
	// while it waited.
	time.Sleep(100 * time.Millisecond)
	go func() { _ = pool.Live("cluster-a") }()
	time.Sleep(100 * time.Millisecond)

	answered := make(chan int, 1)
	go func() { answered <- pool.Live("cluster-b") }()
	select {
	case live := <-answered:
		if live != 1 {
			t.Errorf("cluster-b has %d live tunnels, want 1", live)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("asking about cluster-b blocked behind a stalled tunnel to cluster-a")
	}

	req, err := http.NewRequest(http.MethodGet, "https://ignored/v1/models", nil)
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		resp, err := pool.RoundTripper("cluster-b").RoundTrip(req)
		if err == nil {
			_ = resp.Body.Close()
		}
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("request to cluster-b: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a request to cluster-b blocked behind a stalled tunnel to cluster-a")
	}
}
