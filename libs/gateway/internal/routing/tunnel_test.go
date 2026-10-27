package routing

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

// recordingFactory answers with the cluster it was built for, so a test can tell
// which handler a request reached.
func recordingFactory(targets *[]Target) ProxyFactory {
	return func(target Target) http.Handler {
		*targets = append(*targets, target)
		cluster := target.ClusterID
		return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("X-Cluster", cluster)
		})
	}
}

func tunnelTable(clusters ...string) plane.Table {
	endpoint := plane.Endpoint{ID: "ep_chat", Name: "chat", RoutingStrategy: "weighted"}
	for i, cluster := range clusters {
		endpoint.Upstreams = append(endpoint.Upstreams, plane.Upstream{
			DeploymentID:   "dep_" + cluster,
			ClusterID:      cluster,
			ClusterName:    cluster,
			AdmissionPhase: "running",
			Weight:         100,
			Role:           "primary",
		})
		_ = i
	}
	return plane.Table{Revision: "1", Endpoints: []plane.Endpoint{endpoint}}
}

// TestAnUpstreamWithNoAddressIsRoutedByClusterId is the point of the
// tunnel world: nothing sets a serving URL, so the cluster id is the routing key.
func TestAnUpstreamWithNoAddressIsRoutedByClusterId(t *testing.T) {
	var targets []Target
	source := &stubSource{tables: []plane.Table{tunnelTable("clu_ap")}}
	router := New(source, recordingFactory(&targets), WithReachability(liveClusters{"clu_ap": 1}))
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}

	if len(targets) != 1 {
		t.Fatalf("targets = %+v, want one", targets)
	}
	if targets[0].ClusterID != "clu_ap" || targets[0].URL != nil {
		t.Fatalf("target = %+v, want the cluster id and no address", targets[0])
	}

	route, err := router.Resolve("chat")
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	picked, err := route.Pick()
	if err != nil {
		t.Fatalf("pick: %v", err)
	}
	if !picked.Tunnelled || picked.ClusterID != "clu_ap" {
		t.Fatalf("picked = %+v", picked)
	}
	rec := httptest.NewRecorder()
	picked.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil))
	if rec.Header().Get("X-Cluster") != "clu_ap" {
		t.Fatalf("handler served cluster %q", rec.Header().Get("X-Cluster"))
	}
}

// TestReachabilityDecidesWhichClustersAreCandidates covers the 503-not-502 rule
// from both sides: a cluster with no tunnel is not a candidate at all, so an
// endpoint whose only cluster has none reports no capacity instead of handing a
// request to a proxy with nowhere to send it.
func TestReachabilityDecidesWhichClustersAreCandidates(t *testing.T) {
	cases := []struct {
		name    string
		live    liveClusters
		want    string
		wantErr error
	}{
		{name: "both reachable", live: liveClusters{"clu_ap": 1, "clu_us": 2}},
		{name: "only one reachable", live: liveClusters{"clu_us": 1}, want: "clu_us"},
		{name: "none reachable", live: liveClusters{}, wantErr: ErrNoUpstream},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var targets []Target
			source := &stubSource{tables: []plane.Table{tunnelTable("clu_ap", "clu_us")}}
			router := New(source, recordingFactory(&targets), WithReachability(tc.live))
			if err := router.Load(context.Background()); err != nil {
				t.Fatalf("load: %v", err)
			}
			route, _ := router.Resolve("chat")

			for i := 0; i < 6; i++ {
				picked, err := route.Pick()
				if tc.wantErr != nil {
					if !errors.Is(err, tc.wantErr) {
						t.Fatalf("pick = %v, want %v", err, tc.wantErr)
					}
					continue
				}
				if err != nil {
					t.Fatalf("pick: %v", err)
				}
				if tc.want != "" && picked.ClusterID != tc.want {
					t.Fatalf("picked %q, want only %q", picked.ClusterID, tc.want)
				}
			}
		})
	}
}

// TestWithoutAPoolAnAddresslessUpstreamIsNeverPicked keeps the behaviour a
// gateway with tunnels switched off already had: such an upstream used to be
// skipped at load, and it must still never receive a request.
func TestWithoutAPoolAnAddresslessUpstreamIsNeverPicked(t *testing.T) {
	source := &stubSource{tables: []plane.Table{tunnelTable("clu_ap")}}
	router := New(source, factory())
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	route, _ := router.Resolve("chat")
	if _, err := route.Pick(); !errors.Is(err, ErrNoUpstream) {
		t.Fatalf("pick = %v, want ErrNoUpstream", err)
	}
}

// TestTunnelledUpstreamsInDifferentClustersDoNotShareAHandler is the bug the
// proxy key exists to prevent: with the cluster left out of the key, every
// addressless upstream sharing a credential collapses onto one handler, and that
// handler forwards to exactly one cluster.
func TestTunnelledUpstreamsInDifferentClustersDoNotShareAHandler(t *testing.T) {
	var targets []Target
	source := &stubSource{tables: []plane.Table{tunnelTable("clu_ap", "clu_us")}}
	router := New(source, recordingFactory(&targets), WithReachability(liveClusters{"clu_ap": 1, "clu_us": 1}))
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(router.proxies) != 2 {
		t.Fatalf("proxies = %d, want one per cluster", len(router.proxies))
	}

	route, _ := router.Resolve("chat")
	served := map[string]string{}
	for _, upstream := range route.Upstreams {
		rec := httptest.NewRecorder()
		upstream.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil))
		served[upstream.ClusterID] = rec.Header().Get("X-Cluster")
	}
	for cluster, reached := range served {
		if cluster != reached {
			t.Fatalf("the handler for %s forwards to %s", cluster, reached)
		}
	}
}

func TestClusterReachReportsTheTableAgainstTheTunnelsHeld(t *testing.T) {
	source := &stubSource{tables: []plane.Table{{
		Revision: "1",
		Endpoints: []plane.Endpoint{{
			ID:   "ep_chat",
			Name: "chat",
			Upstreams: []plane.Upstream{
				{DeploymentID: "dep_1", ClusterID: "clu_ap", ClusterName: "ap", AdmissionPhase: "running", Weight: 100},
				{DeploymentID: "dep_2", ClusterID: "clu_us", ClusterName: "us", AdmissionPhase: "running", Weight: 100},
				{DeploymentID: "dep_3", ClusterID: "clu_eu", ClusterName: "eu", AdmissionPhase: "running", Weight: 100, URL: "http://eu.svc:8000"},
			},
		}},
	}}}
	router := New(source, factory(), WithReachability(liveClusters{"clu_ap": 2}))
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}

	want := []ClusterReach{
		{ClusterID: "clu_ap", ClusterName: "ap", Tunnelled: true, Reachable: true},
		{ClusterID: "clu_eu", ClusterName: "eu", Tunnelled: false, Reachable: true},
		{ClusterID: "clu_us", ClusterName: "us", Tunnelled: true, Reachable: false},
	}
	got := router.ClusterReach()
	if len(got) != len(want) {
		t.Fatalf("reach = %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("reach[%d] = %+v, want %+v", i, got[i], want[i])
		}
	}
}

func TestUpstreamsWithNoClusterAreSkipped(t *testing.T) {
	source := &stubSource{tables: []plane.Table{{
		Revision: "1",
		Endpoints: []plane.Endpoint{{
			Name: "chat",
			Upstreams: []plane.Upstream{
				{DeploymentID: "dep_1", Weight: 100, AdmissionPhase: "running"},
				{DeploymentID: "dep_2", ClusterID: "clu_ap", ClusterName: "ap", Weight: 100, AdmissionPhase: "running"},
			},
		}},
	}}}
	router := New(source, factory(), WithReachability(liveClusters{"clu_ap": 1}))
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	route, _ := router.Resolve("chat")
	if len(route.Upstreams) != 1 || route.Upstreams[0].ClusterID != "clu_ap" {
		t.Fatalf("upstreams = %+v", route.Upstreams)
	}
}
