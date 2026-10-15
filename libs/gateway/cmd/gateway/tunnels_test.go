package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

func TestTunnelSettingsReadTheEnvironment(t *testing.T) {
	t.Setenv("HANGAR_GATEWAY_TUNNEL_MAX_PER_CLUSTER", "8")
	t.Setenv("HANGAR_GATEWAY_TUNNEL_MAX", "512")
	t.Setenv("HANGAR_GATEWAY_TUNNEL_BODY_IDLE_TIMEOUT", "off")
	t.Setenv("HANGAR_GATEWAY_TUNNEL_CACHE_TTL", "30s")
	t.Setenv("HANGAR_GATEWAY_TUNNEL_CACHE_GRACE", "5m")
	t.Setenv("HANGAR_GATEWAY_TUNNEL_REVERIFY_INTERVAL", "20s")
	t.Setenv("HANGAR_GATEWAY_TUNNEL_MAX_HANDSHAKES", "64")
	t.Setenv("HANGAR_GATEWAY_TUNNEL_MAX_HANDSHAKES_PER_SOURCE", "OFF")
	t.Setenv("HANGAR_GATEWAY_TUNNEL_SOURCE_FAILURES", "")

	settings := tunnelSettingsFromEnv()
	pool := settings.pool(tunnel.PoolConfig{})
	if pool.MaxTunnelsPerCluster != 8 || pool.MaxTunnels != 512 || pool.BodyIdleTimeout != -1 {
		t.Errorf("pool config = %+v", pool)
	}
	listener := settings.listener(tunnel.ListenerConfig{GatewayID: "gw-0"})
	want := tunnel.ListenerConfig{
		GatewayID:              "gw-0",
		CacheTTL:               30 * time.Second,
		CacheGrace:             5 * time.Minute,
		ReverifyInterval:       20 * time.Second,
		MaxHandshakes:          64,
		MaxHandshakesPerSource: -1,
	}
	if fmt.Sprintf("%+v", listener) != fmt.Sprintf("%+v", want) {
		t.Errorf("listener config = %+v, want %+v", listener, want)
	}
}

// A malformed value keeps the package default rather than stopping the
// gateway, like every other setting it reads, and a negative one cannot turn
// re-verification off.
func TestUnreadableTunnelSettingsKeepTheDefaults(t *testing.T) {
	t.Setenv("HANGAR_GATEWAY_TUNNEL_MAX", "lots")
	t.Setenv("HANGAR_GATEWAY_TUNNEL_REVERIFY_INTERVAL", "-1s")
	t.Setenv("HANGAR_GATEWAY_TUNNEL_SOURCE_FAILURES", "-3")

	settings := tunnelSettingsFromEnv()
	if settings.maxTunnels != 0 || settings.reverify != 0 || settings.sourceFailures != 0 {
		t.Errorf("settings = %+v, want every unreadable value left at zero", settings)
	}
}

type rejectAll struct{}

func (rejectAll) VerifyTunnelToken(context.Context, string) (tunnel.Identity, error) {
	return tunnel.Identity{}, fmt.Errorf("%w: test", tunnel.ErrRejected)
}

// brokenListener fails every Accept with an error that is not temporary.
type brokenListener struct{ net.Listener }

func (brokenListener) Accept() (net.Conn, error) {
	return nil, errors.New("accept: the socket is gone")
}

// TestAFailedTunnelListenerIsBoundAgain pins that a tunnel accept error no
// longer stops the process: the open tunnels do not need the accept socket,
// and exiting would sever all of them.
func TestAFailedTunnelListenerIsBoundAgain(t *testing.T) {
	pool, err := tunnel.NewPool(tunnel.PoolConfig{})
	if err != nil {
		t.Fatal(err)
	}
	listener, err := tunnel.NewListener(tunnel.ListenerConfig{Pool: pool, Verifier: rejectAll{}})
	if err != nil {
		t.Fatal(err)
	}
	first, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}

	rebound := make(chan string, 1)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		keepServing(ctx, listener, brokenListener{first}, func() (net.Listener, error) {
			next, err := net.Listen("tcp", "127.0.0.1:0")
			if err == nil {
				rebound <- next.Addr().String()
			}
			return next, err
		})
	}()
	t.Cleanup(func() {
		cancel()
		_ = listener.Close()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("keepServing did not return after shutdown")
		}
	})

	var addr string
	select {
	case addr = <-rebound:
	case <-time.After(10 * time.Second):
		t.Fatal("the tunnel listener was not bound again")
	}
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatalf("dial the rebound listener: %v", err)
	}
	defer conn.Close()
	handshake, stop := context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	if _, err := tunnel.Handshake(handshake, conn, tunnel.Hello{Token: "junk"}); !errors.Is(err, tunnel.ErrRejected) {
		t.Fatalf("handshake on the rebound listener = %v, want the verifier's refusal", err)
	}
}
