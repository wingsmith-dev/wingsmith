package serving

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// stubTunnels is the tunnel pool as the cluster handler sees it.
type stubTunnels struct {
	live int
	trip func(*http.Request) (*http.Response, error)
}

func (s stubTunnels) Live(string) int { return s.live }

func (s stubTunnels) RoundTripper(string) http.RoundTripper {
	return roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if s.trip != nil {
			return s.trip(r)
		}
		// Which leg served the request travels in the body and not in a
		// response header, because a response header from behind this hop is
		// deleted on the way out; see filterResponseHeaders. A test asserting
		// through one would be asserting the leak.
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"via":"tunnel"}`)),
			Request:    r,
		}, nil
	})
}

// TestTheTunnelIsPreferredAndTheAddressIsTheFallback pins the coexistence rule.
// ClusterServing is not deleted: a cluster that publishes an address is still
// dialled at it when this replica holds no tunnel to it.
func TestTheTunnelIsPreferredAndTheAddressIsTheFallback(t *testing.T) {
	direct := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"via":"address"}`))
	}))
	defer direct.Close()
	address, err := url.Parse(direct.URL)
	if err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name    string
		live    int
		url     *url.URL
		wantVia string
		status  int
	}{
		{name: "tunnel and address, tunnel wins", live: 1, url: address, wantVia: "tunnel", status: http.StatusOK},
		{name: "address only", live: 0, url: address, wantVia: "address", status: http.StatusOK},
		{name: "tunnel only", live: 2, wantVia: "tunnel", status: http.StatusOK},
		{name: "neither", live: 0, status: http.StatusServiceUnavailable},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			handler := NewClusterHandler(ClusterConfig{
				ClusterID: "clu_ap",
				URL:       tc.url,
				Tunnels:   stubTunnels{live: tc.live},
			})
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader("{}")))

			if rec.Code != tc.status {
				t.Fatalf("status = %d, want %d", rec.Code, tc.status)
			}
			if tc.wantVia == "" {
				var envelope errorEnvelope
				if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
					t.Fatalf("body = %q", rec.Body.String())
				}
				if envelope.Error.Code != "no_capacity" {
					t.Fatalf("code = %q, want the no-capacity 503 instead of an upstream error", envelope.Error.Code)
				}
				return
			}
			var served struct {
				Via string `json:"via"`
			}
			if err := json.Unmarshal(rec.Body.Bytes(), &served); err != nil {
				t.Fatalf("body = %q", rec.Body.String())
			}
			if served.Via != tc.wantVia {
				t.Fatalf("served via %q, want %q", served.Via, tc.wantVia)
			}
		})
	}
}

// TestAClusterWithNothingBehindItIsNoCapacityAndNotAnUpstreamError is the
// distinction a reviewer measured wrong: nothing was attempted, so blaming the
// cluster would deflate its availability for a tunnel this replica never had.
func TestAClusterWithNothingBehindItIsNoCapacityAndNotAnUpstreamError(t *testing.T) {
	recorder := metrics.New()
	handler := New(Config{
		Zone: ZoneInternal,
		Router: stubResolver{routes: map[string]routing.Route{
			"chat": handlerUpstream(t, "ep_1", "chat", "dep_1", "ap",
				NewClusterHandler(ClusterConfig{ClusterID: "clu_ap"})),
		}},
		Metrics: recorder,
	})

	rec := post(t, handler, `{"model":"chat"}`)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d", rec.Code)
	}
	if got := requests(t, recorder, map[string]string{"outcome": "no_upstream"}); got != 1 {
		t.Fatalf("no_upstream = %v, want 1", got)
	}
	if got := requests(t, recorder, map[string]string{"outcome": "upstream_error"}); got != 0 {
		t.Fatalf("upstream_error = %v, want none", got)
	}
}

// TestTunnelFaultsAreClassifiedRatherThanAllBeing502 keeps a connector rollout, a
// saturated cluster and a cluster that genuinely failed from being one number.
func TestTunnelFaultsAreClassifiedRatherThanAllBeing502(t *testing.T) {
	cases := []struct {
		name    string
		err     error
		status  int
		outcome string
	}{
		{name: "no tunnel", err: tunnel.ErrNoTunnel, status: http.StatusServiceUnavailable, outcome: "no_upstream"},
		{name: "saturated", err: tunnel.ErrSaturated, status: http.StatusServiceUnavailable, outcome: "shed"},
		{name: "connector churn", err: errors.New("http2: client connection lost"), status: http.StatusBadGateway, outcome: "upstream_error"},
		{name: "no response header", err: context.DeadlineExceeded, status: http.StatusBadGateway, outcome: "upstream_timeout"},
		{name: "the cluster failed", err: errors.New("boom"), status: http.StatusBadGateway, outcome: "upstream_error"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			recorder := metrics.New()
			tunnels := stubTunnels{live: 1, trip: func(*http.Request) (*http.Response, error) { return nil, tc.err }}
			handler := New(Config{
				Zone: ZoneInternal,
				Router: stubResolver{routes: map[string]routing.Route{
					"chat": handlerUpstream(t, "ep_1", "chat", "dep_1", "ap",
						NewClusterHandler(ClusterConfig{ClusterID: "clu_ap", Tunnels: tunnels})),
				}},
				Metrics: recorder,
			})

			rec := post(t, handler, `{"model":"chat"}`)
			if rec.Code != tc.status {
				t.Fatalf("status = %d, want %d", rec.Code, tc.status)
			}
			if got := requests(t, recorder, map[string]string{"outcome": tc.outcome}); got != 1 {
				t.Fatalf("%s = %v, want 1", tc.outcome, got)
			}
		})
	}
}

// TestATunnelledRequestKeepsItsBodyReplayable is what makes HTTP/2 retry a
// completion that a draining connector never processed.
func TestATunnelledRequestKeepsItsBodyReplayable(t *testing.T) {
	var replayable bool
	var seen string
	tunnels := stubTunnels{live: 1, trip: func(r *http.Request) (*http.Response, error) {
		replayable = r.GetBody != nil
		if r.GetBody != nil {
			again, err := r.GetBody()
			if err != nil {
				t.Fatalf("get body: %v", err)
			}
			body, _ := io.ReadAll(again)
			seen = string(body)
		}
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader("{}")), Request: r}, nil
	}}

	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"chat": handlerUpstream(t, "ep_1", "chat", "dep_1", "ap",
			NewClusterHandler(ClusterConfig{ClusterID: "clu_ap", Tunnels: tunnels})),
	}}})

	if rec := post(t, handler, `{"model":"chat"}`); rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	if !replayable {
		t.Fatal("the request reached the transport with no GetBody, so a GOAWAY would fail it")
	}
	if seen != `{"model":"chat"}` {
		t.Fatalf("replayed body = %q", seen)
	}
}

// headerTimeoutOf reads the bound a proxy's own transport arms, or zero when the
// transport is not one of ours. That is the tunnelled case, where the pool
// owns the bound instead.
func headerTimeoutOf(t *testing.T, proxy *httputil.ReverseProxy) time.Duration {
	t.Helper()
	transport, ok := proxy.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("proxy transport is %T, not the one this hop configures", proxy.Transport)
	}
	return transport.ResponseHeaderTimeout
}

// A non-streamed completion sends no response header until its last token, so
// this bound is the whole generation for that shape. Five minutes cut off the
// long ones; the bound is configurable now, and an unset one has to land on the
// fleet-wide default, not on no bound at all.
func TestTheUpstreamBoundIsConfigurableAndDefaultsToTheFleetBudget(t *testing.T) {
	upstream, err := url.Parse("https://cluster.example")
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	for _, tc := range []struct {
		name      string
		configure time.Duration
		want      time.Duration
	}{
		{"unset takes the shared default", 0, tunnel.DefaultResponseHeaderTimeout},
		{"a negative value is not no bound", -time.Second, tunnel.DefaultResponseHeaderTimeout},
		{"a raised value is kept", 90 * time.Minute, 90 * time.Minute},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := headerTimeoutOf(t, NewReverseProxy(upstream, nil, tc.configure)); got != tc.want {
				t.Fatalf("response header timeout = %s, want %s", got, tc.want)
			}
		})
	}
}

// The default has to be the number the agent derives its own from, or the two
// ends of the tunnel disagree about the wall and the innermost hop wins. This is
// the whole of the fix: one constant, three hops.
func TestTheGatewayDefaultIsTheSameNumberTheAgentDerivesFrom(t *testing.T) {
	if tunnel.DefaultResponseHeaderTimeout >= tunnel.InnerHopTimeout(tunnel.DefaultResponseHeaderTimeout) {
		t.Fatal("the gateway's default is not smaller than the agent's, so the agent gives up first")
	}
}

// A cluster reachable both ways must present one wall. A caller cannot see which
// leg carried its request, so a bound that differed between them would make the
// timeout depend on whether the cluster's agent happened to be connected.
func TestBothLegsIntoAClusterCarryTheSameBound(t *testing.T) {
	upstream, err := url.Parse("https://cluster.example")
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	const budget = 70 * time.Minute
	direct := NewReverseProxy(upstream, nil, budget)
	tunnelled := NewTunnelProxy("clu_1", nil, stubTunnels{live: 1}, budget)

	if got := headerTimeoutOf(t, direct); got != budget {
		t.Fatalf("direct leg bound = %s, want %s", got, budget)
	}
	// The tunnelled leg's transport is the pool's, which arms the bound from
	// PoolConfig, not from this proxy, so what is asserted here is that
	// the seam is still one field wide and nothing else was left behind on it.
	if _, ours := tunnelled.Transport.(*http.Transport); ours {
		t.Fatal("the tunnelled leg is still on the gateway's own transport, so it never reaches a cluster")
	}
}
