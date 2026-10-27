package tunnel

import (
	"context"
	"errors"
	"net"
	"net/http"
	"sync"

	"golang.org/x/net/http2"
)

// ErrConnectorClosed is returned by Serve after Shutdown or Close.
var ErrConnectorClosed = errors.New("tunnel: connector closed")

// ConnectorConfig configures the cluster end of a tunnel.
type ConnectorConfig struct {
	// Handler answers every request that arrives down a tunnel. In a cluster it
	// is a reverse proxy to the local serving Service; nothing about this
	// package assumes that.
	Handler http.Handler
	// Limits are the HTTP/2 settings this end advertises. Zero fields default.
	Limits Limits
	// BaseContext is the parent of every handler context. Nil means
	// context.Background. Cancelling it is an abort, not a drain: use Shutdown
	// to drain.
	BaseContext context.Context
}

// Connector serves HTTP/2 on connections the cluster dialled out.
//
// The dial is the caller's: this type is handed a net.Conn that is already
// connected and, in production, already through a TLS handshake and an
// authentication exchange. It exists to be testable over a net.Pipe.
type Connector struct {
	h1      *http.Server
	h2      *http2.Server
	baseCtx context.Context

	mu     sync.Mutex
	closed bool
	// conns is every tunnel currently being served. http.Server tracks the
	// connections it accepted; a tunnel arrives from a dialler it never saw, so
	// neither its Shutdown nor its Close can reach one.
	conns map[net.Conn]struct{}
	// serving counts connections inside ServeConn, so Shutdown can wait for the
	// GOAWAY it sends to actually drain.
	serving sync.WaitGroup
}

// NewConnector builds the cluster end.
//
// http2.ConfigureServer is what registers the graceful-shutdown hook that turns
// Shutdown into a GOAWAY on every live tunnel. Constructing an http2.Server
// directly skips it, and a rollout would then sever in-flight streams instead
// of draining them, and every one of those is somebody's half-written completion.
func NewConnector(cfg ConnectorConfig) (*Connector, error) {
	if cfg.Handler == nil {
		return nil, errors.New("tunnel: connector needs a handler")
	}
	limits := cfg.Limits.withDefaults()
	baseCtx := cfg.BaseContext
	if baseCtx == nil {
		baseCtx = context.Background()
	}

	h2 := &http2.Server{
		MaxConcurrentStreams:         limits.MaxConcurrentStreams,
		MaxUploadBufferPerConnection: limits.ConnWindow,
		MaxUploadBufferPerStream:     limits.StreamWindow,
		MaxReadFrameSize:             limits.MaxReadFrameSize,
		ReadIdleTimeout:              limits.ReadIdleTimeout,
		PingTimeout:                  limits.PingTimeout,
		WriteByteTimeout:             limits.WriteByteTimeout,
	}
	// No IdleTimeout, no ReadTimeout. A tunnel with no traffic on it is a
	// cluster nobody is asking for right now, and closing it would mean the
	// first request after a quiet spell pays for a redial. Liveness is the
	// PING, which costs nothing and does not lie.
	h1 := &http.Server{
		Handler:     cfg.Handler,
		IdleTimeout: 0,
		ReadTimeout: 0,
	}
	if err := http2.ConfigureServer(h1, h2); err != nil {
		return nil, err
	}

	return &Connector{
		h1:      h1,
		h2:      h2,
		baseCtx: baseCtx,
		conns:   make(map[net.Conn]struct{}),
	}, nil
}

// Serve runs HTTP/2 over one outbound connection and blocks until it ends.
//
// It always closes conn.
func (c *Connector) Serve(conn net.Conn) error {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		_ = conn.Close()
		return ErrConnectorClosed
	}
	c.serving.Add(1)
	c.conns[conn] = struct{}{}
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		delete(c.conns, conn)
		c.mu.Unlock()
		c.serving.Done()
	}()

	// TODO: wrap Handler here to set the cause trailer the gateway wants.
	// A stream that fails because this pod is going away and a stream that fails
	// because the serving runtime behind it went away are the same StreamError at
	// the gateway; see the matching TODO in classify.go. The seam is a trailer
	// set on the way out of the handler: announced with w.Header().Set("Trailer",
	// ...) before the first write and filled in afterwards. It is additive, so no
	// wire break and no protocol version, and the gateway's behaviour when it is
	// absent is today's behaviour. Deep attribution (OOM against evicted against
	// crashed) is not this seam's: it is a join against the operator's observed
	// state on cluster and deployment, which already exists.
	c.h2.ServeConn(conn, &http2.ServeConnOpts{
		Context:    c.baseCtx,
		BaseConfig: c.h1,
		Handler:    c.h1.Handler,
	})
	return nil
}

// Shutdown sends GOAWAY on every live tunnel and waits for the streams already
// accepted to finish.
//
// An agent is a Deployment that gets rolled, so this is the ordinary case
// instead of the exceptional one: the pod is asked to stop while it is holding
// streams that are minutes from finishing. GOAWAY tells the gateway which
// streams were never processed: those the gateway retries down another tunnel,
// which is the whole reason a cluster holds more than one.
//
// It returns ctx.Err() if the drain outlasts the context; the connections are
// left alone in that case, and Close severs them.
func (c *Connector) Shutdown(ctx context.Context) error {
	c.mu.Lock()
	c.closed = true
	c.mu.Unlock()

	// Runs the hook ConfigureServer registered, which is what reaches every
	// active serverConn. It does not wait for tunnels, because it never
	// accepted any.
	_ = c.h1.Shutdown(ctx)

	done := make(chan struct{})
	go func() {
		c.serving.Wait()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Close severs every tunnel without draining. It is the deadline behind
// Shutdown, not an alternative to it.
func (c *Connector) Close() error {
	c.mu.Lock()
	c.closed = true
	conns := make([]net.Conn, 0, len(c.conns))
	for conn := range c.conns {
		conns = append(conns, conn)
	}
	c.mu.Unlock()

	err := c.h1.Close()
	for _, conn := range conns {
		_ = conn.Close()
	}
	c.serving.Wait()
	return err
}
