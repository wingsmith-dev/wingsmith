package agent

import (
	"context"
	"io"
	"net"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// shutdownRig is the agent's process wiring with the network faked: a supervisor dialling a fake
// gateway that adopts every tunnel into a real pool, and a real connector serving handler.
type shutdownRig struct {
	pool       *tunnel.Pool
	connector  *tunnel.Connector
	supervisor *Supervisor
	cancel     context.CancelFunc
	supervised chan struct{}
	dials      *atomic.Int64
}

func startShutdownRig(t *testing.T, handler http.Handler) *shutdownRig {
	t.Helper()
	pool, err := tunnel.NewPool(tunnel.PoolConfig{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	connector, err := tunnel.NewConnector(tunnel.ConnectorConfig{Handler: handler})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connector.Close() })

	dials := &atomic.Int64{}
	gw := startGateway(t, &gateway{accepted: func(conn net.Conn) {
		dials.Add(1)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, _ = pool.Adopt(ctx, "clu_test", conn)
	}})

	supervisor, err := NewSupervisor(SupervisorConfig{
		Gateway:         gw.addr,
		ConnsPerGateway: 1,
		Dial:            testDial(t, gw.addr),
		Server:          connector,
		BackoffMin:      time.Millisecond,
		BackoffMax:      5 * time.Millisecond,
		Log:             quietLogger(),
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	rig := &shutdownRig{pool: pool, connector: connector, supervisor: supervisor, cancel: cancel,
		supervised: make(chan struct{}), dials: dials}
	go func() {
		defer close(rig.supervised)
		supervisor.Run(ctx)
	}()
	t.Cleanup(func() {
		cancel()
		<-rig.supervised
	})
	eventually(t, "the tunnel", func() bool { return pool.Live("clu_test") == 1 })
	return rig
}

// stop is the agent's SIGTERM path: stop dialling, then drain, then sever what is left.
func (r *shutdownRig) stop(t *testing.T, drain time.Duration) error {
	t.Helper()
	r.cancel()
	ctx, cancel := context.WithTimeout(context.Background(), drain)
	defer cancel()
	err := r.connector.Shutdown(ctx)
	if err != nil {
		_ = r.connector.Close()
	}
	select {
	case <-r.supervised:
	case <-time.After(5 * time.Second):
		t.Fatal("the supervisor did not return after the connector stopped")
	}
	return err
}

// TestShutdownDrainsAStreamInFlight is the ordinary rollout of an agent: a generation that is
// halfway through when SIGTERM arrives finishes, and the agent does not dial back in afterwards.
func TestShutdownDrainsAStreamInFlight(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	rig := startShutdownRig(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("first "))
		_ = http.NewResponseController(w).Flush()
		close(started)
		<-release
		_, _ = w.Write([]byte("last"))
	}))

	body := make(chan string, 1)
	go func() {
		req, _ := http.NewRequest(http.MethodGet, "https://ignored/v1/chat/completions", nil)
		resp, err := rig.pool.RoundTripper("clu_test").RoundTrip(req)
		if err != nil {
			body <- "error: " + err.Error()
			return
		}
		defer resp.Body.Close()
		read, err := io.ReadAll(resp.Body)
		if err != nil {
			body <- "error: " + err.Error()
			return
		}
		body <- string(read)
	}()
	<-started
	dialsBefore := rig.dials.Load()

	stopped := make(chan error, 1)
	go func() { stopped <- rig.stop(t, 10*time.Second) }()
	// The drain has to be waiting on the stream, not finished without it.
	select {
	case err := <-stopped:
		t.Fatalf("shutdown returned %v with a stream still in flight", err)
	case <-time.After(100 * time.Millisecond):
	}
	close(release)

	if err := <-stopped; err != nil {
		t.Fatalf("drain: %v", err)
	}
	if got := <-body; got != "first last" {
		t.Errorf("the in-flight stream ended with %q, want it finished", got)
	}
	if got := rig.dials.Load(); got != dialsBefore {
		t.Errorf("the agent dialled %d more times while shutting down", got-dialsBefore)
	}
	if got := rig.supervisor.Tunnels(); got != 0 {
		t.Errorf("%d tunnels counted after shutdown", got)
	}
}

// TestShutdownPastItsDeadlineSevers is the other half: a stream that outlasts the drain timeout is
// cut, and the agent still exits rather than waiting on it.
func TestShutdownPastItsDeadlineSevers(t *testing.T) {
	started := make(chan struct{})
	rig := startShutdownRig(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_ = http.NewResponseController(w).Flush()
		close(started)
		<-r.Context().Done()
	}))

	done := make(chan error, 1)
	go func() {
		req, _ := http.NewRequest(http.MethodGet, "https://ignored/v1/chat/completions", nil)
		resp, err := rig.pool.RoundTripper("clu_test").RoundTrip(req)
		if err != nil {
			done <- err
			return
		}
		defer resp.Body.Close()
		_, err = io.ReadAll(resp.Body)
		done <- err
	}()
	<-started

	if err := rig.stop(t, 100*time.Millisecond); err == nil {
		t.Fatal("a drain with a stream that never ends reported success")
	}
	select {
	case err := <-done:
		if err == nil {
			t.Error("a severed stream read as complete")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the stream was not severed after the drain deadline")
	}
}
