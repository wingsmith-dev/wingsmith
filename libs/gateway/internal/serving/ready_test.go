package serving

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

type stubReach []routing.ClusterReach

func (s stubReach) ClusterReach() []routing.ClusterReach { return s }

type coverageLog struct{ samples []metrics.TunnelSample }

func (c *coverageLog) TunnelCoverage(samples []metrics.TunnelSample) { c.samples = samples }

func TestReadinessFollowsTheTunnelsThisReplicaHolds(t *testing.T) {
	cases := []struct {
		name     string
		clusters stubReach
		draining bool
		want     bool
	}{
		{name: "an empty table is ready", want: true},
		{
			name:     "no tunnel to the only cluster",
			clusters: stubReach{{ClusterID: "clu_ap", Tunnelled: true}},
		},
		{
			name:     "a tunnel to one of two clusters",
			clusters: stubReach{{ClusterID: "clu_ap", Tunnelled: true, Reachable: true}, {ClusterID: "clu_us", Tunnelled: true}},
			want:     true,
		},
		{
			name:     "an address needs no tunnel",
			clusters: stubReach{{ClusterID: "clu_eu", Reachable: true}},
			want:     true,
		},
		{
			name:     "draining is never ready",
			clusters: stubReach{{ClusterID: "clu_ap", Tunnelled: true, Reachable: true}},
			draining: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			readiness := NewReadiness(tc.clusters, nil)
			if tc.draining {
				readiness.Drain()
			}
			got, detail := readiness.Ready()
			if got != tc.want {
				t.Fatalf("ready = %v (%s), want %v", got, detail, tc.want)
			}
		})
	}
}

// TestLivenessAnswersWhileReadinessDoesNot is the split. A replica with no tunnel
// must be taken out of the Service and must not be restarted: restarting is
// exactly what will not produce a tunnel.
func TestLivenessAnswersWhileReadinessDoesNot(t *testing.T) {
	readiness := NewReadiness(stubReach{{ClusterID: "clu_ap", Tunnelled: true}}, nil)
	handler := New(Config{Router: stubResolver{}, Ready: readiness})

	live := httptest.NewRecorder()
	handler.ServeHTTP(live, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if live.Code != http.StatusOK {
		t.Fatalf("healthz = %d, liveness must not depend on coverage", live.Code)
	}

	ready := httptest.NewRecorder()
	handler.ServeHTTP(ready, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if ready.Code != http.StatusServiceUnavailable {
		t.Fatalf("readyz = %d, want 503 while this replica holds no tunnel", ready.Code)
	}
	if !strings.Contains(ready.Body.String(), "no tunnel") {
		t.Fatalf("readyz said %q, which does not say why", ready.Body.String())
	}
}

func TestReadinessIsCountedButNotObservedAsTraffic(t *testing.T) {
	recorder := metrics.New()
	handler := New(Config{Router: stubResolver{}, Metrics: recorder, Ready: NewReadiness(stubReach{}, nil)})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("readyz = %d", rec.Code)
	}
	if got := len(gathered(t, recorder, "hangar_gateway_requests_total")); got != 0 {
		t.Fatalf("readyz produced %d series, want none", got)
	}
}

func TestReadinessReportsCoverageToTheMetrics(t *testing.T) {
	log := &coverageLog{}
	readiness := NewReadiness(stubReach{
		{ClusterID: "clu_ap", ClusterName: "ap", Tunnelled: true, Reachable: true},
		{ClusterID: "clu_us", ClusterName: "us", Tunnelled: true},
	}, log)

	if ok, _ := readiness.Ready(); !ok {
		t.Fatal("one reachable cluster is ready")
	}
	if len(log.samples) != 2 {
		t.Fatalf("samples = %+v", log.samples)
	}
	if log.samples[0].ClusterID != "clu_ap" || !log.samples[0].Reachable {
		t.Fatalf("samples[0] = %+v", log.samples[0])
	}
	if log.samples[1].ClusterID != "clu_us" || log.samples[1].Reachable {
		t.Fatalf("samples[1] = %+v", log.samples[1])
	}
}
