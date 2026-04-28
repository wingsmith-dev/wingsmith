package serving

import (
	"context"
	"io"
	"net"
	"net/http"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// TestARequestTravelsDownARealTunnel is the seam end to end, with no stubs on it:
// a connector serving HTTP/2 on a connection it dialled, the pool holding it, and
// the ordinary gateway handler forwarding a completion down it.
func TestARequestTravelsDownARealTunnel(t *testing.T) {
	pool, err := tunnel.NewPool(tunnel.PoolConfig{})
	if err != nil {
		t.Fatalf("new pool: %v", err)
	}
	t.Cleanup(func() { _ = pool.Close() })

	inCluster := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("X-Seen-Path", r.URL.Path)
		_, _ = w.Write([]byte("data: " + string(body) + "\n\n"))
		_, _ = w.Write([]byte("data: [DONE]\n\n"))
	})
	connector, err := tunnel.NewConnector(tunnel.ConnectorConfig{Handler: inCluster})
	if err != nil {
		t.Fatalf("new connector: %v", err)
	}
	t.Cleanup(func() { _ = connector.Close() })

	gatewaySide, connectorSide := loopbackPair(t)
	go func() { _ = connector.Serve(connectorSide) }()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := pool.Adopt(ctx, "clu_ap", gatewaySide); err != nil {
		t.Fatalf("adopt: %v", err)
	}

	recorder := metrics.New()
	handler := New(Config{
		Router: stubResolver{routes: map[string]routing.Route{
			"chat": handlerUpstream(t, "ep_1", "chat", "dep_1", "ap",
				NewClusterHandler(ClusterConfig{ClusterID: "clu_ap", Tunnels: pool})),
		}},
		Metrics: recorder,
	})

	rec := post(t, handler, `{"model":"chat"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body %q", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("X-Seen-Path") != "/v1/chat/completions" {
		t.Fatalf("the cluster saw path %q", rec.Header().Get("X-Seen-Path"))
	}
	want := "data: {\"model\":\"chat\"}\n\ndata: [DONE]\n\n"
	if rec.Body.String() != want {
		t.Fatalf("body = %q, want %q", rec.Body.String(), want)
	}
	if got := requests(t, recorder, map[string]string{"outcome": "ok", "cluster": "ap"}); got != 1 {
		t.Fatalf("ok = %v, want the request counted against its cluster", got)
	}
}

// loopbackPair returns the two ends of a loopback TCP connection: the end the
// gateway accepted and the end the connector dialled.
func loopbackPair(t *testing.T) (gatewaySide, connectorSide net.Conn) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer func() { _ = listener.Close() }()

	type accepted struct {
		conn net.Conn
		err  error
	}
	inbound := make(chan accepted, 1)
	go func() {
		conn, err := listener.Accept()
		inbound <- accepted{conn, err}
	}()

	dialled, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	result := <-inbound
	if result.err != nil {
		t.Fatalf("accept: %v", result.err)
	}
	t.Cleanup(func() {
		_ = dialled.Close()
		_ = result.conn.Close()
	})
	return result.conn, dialled
}
