package tunnel

import (
	"context"
	"sync"
	"time"

	"golang.org/x/net/http2"
)

// Conn is one tunnel to one cluster, seen from the gateway.
//
// A cluster holds several. That is not redundancy for its own sake: it is what
// makes an agent rollout survivable, because the retry of a request the
// dying agent never processed has to land somewhere, and it cannot land on
// the connection that just sent GOAWAY.
type Conn struct {
	// ClusterID is the cluster this tunnel reaches. It is the pool key, and the
	// only identity the data path needs.
	ClusterID string
	// RemoteAddr is where the agent dialled from, for logs.
	RemoteAddr string
	// Opened is when the tunnel was adopted.
	Opened time.Time

	cc   *http2.ClientConn
	done chan struct{}
	once sync.Once
}

// Live reports whether this tunnel can take a new request.
//
// It is intentionally not "the socket is open". A connection that has sent or
// received GOAWAY is still readable and still finishing streams, and offering
// it to a new request is how a rollout produces 502s.
func (c *Conn) Live() bool {
	state := c.cc.State()
	return !state.Closed && !state.Closing && state.MaxConcurrentStreams > 0
}

// Streams is how many requests are on this tunnel right now.
func (c *Conn) Streams() int {
	state := c.cc.State()
	return state.StreamsActive + state.StreamsReserved
}

// Capacity is what the agent advertised. Zero means SETTINGS have not
// arrived yet, which is why Live insists on a non-zero value: until then x/net
// assumes 100 streams, and admitting against a number the peer never sent is
// how a per-cluster concurrency cap turns out to be a different number in
// production than the one in the chart.
func (c *Conn) Capacity() int {
	return int(c.cc.State().MaxConcurrentStreams)
}

// Done closes when this tunnel is no longer usable, whatever ended it: GOAWAY
// followed by close, a lost PING, or the socket dying. The accept loop waits on
// it to know an agent has gone and to stop counting it towards coverage.
func (c *Conn) Done() <-chan struct{} { return c.done }

// Shutdown sends GOAWAY and waits for the streams already on this tunnel.
//
// This is the gateway's own rollout: a request-handling replica going away must
// not sever a generation that is halfway through being written to a caller.
func (c *Conn) Shutdown(ctx context.Context) error {
	err := c.cc.Shutdown(ctx)
	c.retire()
	return err
}

// Close severs the tunnel now.
func (c *Conn) Close() error {
	err := c.cc.Close()
	c.retire()
	return err
}

func (c *Conn) retire() {
	c.once.Do(func() { close(c.done) })
}

// watch retires the tunnel once x/net has closed it.
//
// A poll instead of a callback, because there is no exported signal for it.
// ClientConnPool.MarkDead covers most endings promptly, but x/net delays
// MarkDead by up to five seconds for a connection that never carried a request,
// which is exactly the connection a readiness gate is counting.
func (c *Conn) watch(every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-c.done:
			return
		case <-ticker.C:
			if c.cc.State().Closed {
				c.retire()
				return
			}
		}
	}
}
