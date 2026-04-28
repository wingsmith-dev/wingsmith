package routing

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

type stubSource struct {
	tables []plane.Table
	errs   []error
	calls  []string
}

func (s *stubSource) Resolved(_ context.Context, revision string) (plane.Table, error) {
	s.calls = append(s.calls, revision)
	i := len(s.calls) - 1
	if i < len(s.errs) && s.errs[i] != nil {
		return plane.Table{}, s.errs[i]
	}
	if i < len(s.tables) {
		return s.tables[i], nil
	}
	return plane.Table{}, plane.ErrNotModified
}

func factory() ProxyFactory {
	return func(target Target) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			address := ""
			if target.URL != nil {
				address = target.URL.String()
			}
			w.Header().Set("X-Upstream", address)
			w.Header().Set("X-Cluster", target.ClusterID)
			w.Header().Set("X-Credential", target.CredentialID)
		})
	}
}

// liveClusters is a stub tunnel pool: the clusters it names have a tunnel.
type liveClusters map[string]int

func (l liveClusters) Live(clusterID string) int { return l[clusterID] }

func upstream(cluster, deployment, address string, weight int, phase string) plane.Upstream {
	return plane.Upstream{
		DeploymentID:   deployment,
		ClusterID:      cluster,
		ClusterName:    cluster,
		AdmissionPhase: phase,
		URL:            address,
		Weight:         weight,
		Role:           "primary",
	}
}

func TestLoadBuildsRoutesAndKeepsTheRevision(t *testing.T) {
	source := &stubSource{tables: []plane.Table{{
		Revision: "17",
		Endpoints: []plane.Endpoint{{
			Name:            "chat",
			RoutingStrategy: "weighted",
			Upstreams: []plane.Upstream{
				upstream("ap", "dep_a", "http://ap.svc:8000", 100, "running"),
				upstream("us", "dep_b", "http://us.svc:8000", 100, "running"),
			},
		}},
	}}}

	router := New(source, factory())
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	if router.Revision() != "17" {
		t.Fatalf("revision = %q", router.Revision())
	}
	if got := router.Names(); len(got) != 1 || got[0] != "chat" {
		t.Fatalf("names = %v", got)
	}
	route, err := router.Resolve("chat")
	if err != nil || len(route.Upstreams) != 2 {
		t.Fatalf("resolve = %v, %v", route, err)
	}
}

func TestLoadSendsTheCurrentRevisionBackAndKeepsTheTableOnFailure(t *testing.T) {
	source := &stubSource{
		tables: []plane.Table{{
			Revision:  "17",
			Endpoints: []plane.Endpoint{{Name: "chat", Upstreams: []plane.Upstream{upstream("ap", "dep_a", "http://ap.svc:8000", 100, "running")}}},
		}},
		errs: []error{nil, errors.New("plane is down")},
	}

	router := New(source, factory())
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("first load: %v", err)
	}
	if err := router.Load(context.Background()); err == nil {
		t.Fatal("expected the failure to be reported")
	}
	if _, err := router.Resolve("chat"); err != nil {
		t.Fatal("a failed refresh must not erase the table already loaded")
	}
	if source.calls[1] != "17" {
		t.Fatalf("second call sent revision %q, want the one already held", source.calls[1])
	}
}

func TestNotModifiedIsNotAnError(t *testing.T) {
	source := &stubSource{errs: []error{plane.ErrNotModified}}
	router := New(source, factory())
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
}

// TestUnusableAddressesFallBackToTheTunnelWithoutFailingTheRefresh pins the
// coexistence rule. An address that cannot be parsed is not a reason to drop the
// cluster — it has no address, which is the normal case now — and it must not
// take the sibling upstream down with it either.
func TestUnusableAddressesFallBackToTheTunnelWithoutFailingTheRefresh(t *testing.T) {
	source := &stubSource{tables: []plane.Table{{
		Revision: "1",
		Endpoints: []plane.Endpoint{{
			Name: "chat",
			Upstreams: []plane.Upstream{
				upstream("ap", "dep_a", "not a url", 100, "running"),
				upstream("us", "dep_b", "http://us.svc:8000", 100, "running"),
			},
		}},
	}}}

	router := New(source, factory())
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	route, _ := router.Resolve("chat")
	if len(route.Upstreams) != 2 {
		t.Fatalf("upstreams = %v, want both kept", route.Upstreams)
	}
	if !route.Upstreams[0].Tunnelled || route.Upstreams[1].Tunnelled {
		t.Fatalf("tunnelled = %v, %v", route.Upstreams[0].Tunnelled, route.Upstreams[1].Tunnelled)
	}
	// With no pool, the addressless one is not a candidate and the addressed one
	// still answers every request.
	for i := 0; i < 4; i++ {
		picked, err := route.Pick()
		if err != nil || picked.ClusterID != "us" {
			t.Fatalf("pick = %v, %v", picked.ClusterID, err)
		}
	}
}

func TestAnEndpointWithNoUpstreamsIsCapacityNotAbsence(t *testing.T) {
	source := &stubSource{tables: []plane.Table{{
		Revision:  "1",
		Endpoints: []plane.Endpoint{{Name: "chat"}},
	}}}

	router := New(source, factory())
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	route, err := router.Resolve("chat")
	if err != nil {
		t.Fatalf("the endpoint must still resolve: %v", err)
	}
	if _, err := route.Pick(); !errors.Is(err, ErrNoUpstream) {
		t.Fatalf("pick = %v, want ErrNoUpstream", err)
	}
}

func TestPickPrefersReadyUpstreamsButFallsBackToUnreadyOnes(t *testing.T) {
	route := Route{
		Upstreams: []Upstream{
			{ClusterID: "ap", Weight: 100, Ready: false},
			{ClusterID: "us", Weight: 100, Ready: true},
		},
		cursor: &atomic.Uint64{},
	}
	for i := 0; i < 5; i++ {
		picked, err := route.Pick()
		if err != nil || picked.ClusterID != "us" {
			t.Fatalf("pick = %v, %v", picked, err)
		}
	}

	unready := Route{Upstreams: []Upstream{{ClusterID: "ap", Weight: 100}}, cursor: &atomic.Uint64{}}
	picked, err := unready.Pick()
	if err != nil || picked.ClusterID != "ap" {
		t.Fatalf("an unready upstream is better than none: %v, %v", picked, err)
	}
}

func TestWeightedPickHonoursWeightExactly(t *testing.T) {
	route := Route{
		Upstreams: []Upstream{
			{ClusterID: "ap", Weight: 3, Ready: true},
			{ClusterID: "us", Weight: 1, Ready: true},
		},
		cursor: &atomic.Uint64{},
	}
	counts := map[string]int{}
	for i := 0; i < 8; i++ {
		picked, _ := route.Pick()
		counts[picked.ClusterID]++
	}
	if counts["ap"] != 6 || counts["us"] != 2 {
		t.Fatalf("counts = %v, want an exact 3:1 split", counts)
	}
}

func TestFailoverPicksTheDeclaredHead(t *testing.T) {
	source := &stubSource{tables: []plane.Table{{
		Revision: "1",
		Endpoints: []plane.Endpoint{{
			Name:            "chat",
			RoutingStrategy: "failover",
			FailoverOrder:   []string{"dep_b", "dep_a"},
			Upstreams: []plane.Upstream{
				upstream("ap", "dep_a", "http://ap.svc:8000", 100, "running"),
				upstream("us", "dep_b", "http://us.svc:8000", 100, "running"),
			},
		}},
	}}}

	router := New(source, factory())
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	route, _ := router.Resolve("chat")
	for i := 0; i < 3; i++ {
		picked, _ := route.Pick()
		if picked.ClusterID != "us" {
			t.Fatalf("pick = %v, want the head of the failover order every time", picked.ClusterID)
		}
	}
}

func TestUpstreamsSharingAnAddressAndCredentialShareAProxy(t *testing.T) {
	source := &stubSource{tables: []plane.Table{{
		Revision: "1",
		Endpoints: []plane.Endpoint{
			{Name: "chat", Upstreams: []plane.Upstream{upstream("ap", "dep_a", "http://ap.svc:8000", 100, "running")}},
			{Name: "code", Upstreams: []plane.Upstream{upstream("ap", "dep_b", "http://ap.svc:8000", 100, "running")}},
		},
	}}}

	router := New(source, factory())
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(router.proxies) != 1 {
		t.Fatalf("proxies = %d, want one pool shared by both", len(router.proxies))
	}

	chat, _ := router.Resolve("chat")
	picked, _ := chat.Pick()
	rec := httptest.NewRecorder()
	picked.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil))
	if rec.Header().Get("X-Upstream") != "http://ap.svc:8000" {
		t.Fatalf("upstream = %q", rec.Header().Get("X-Upstream"))
	}
}
