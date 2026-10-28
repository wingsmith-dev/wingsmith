package routing

import (
	"context"
	"errors"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/metrics"
	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/plane"
)

type recordingObserver struct {
	applied   int
	unchanged int
	failed    int
	endpoints int
	upstreams []metrics.UpstreamSample
}

func (o *recordingObserver) TableApplied(endpoints int, upstreams []metrics.UpstreamSample) {
	o.applied++
	o.endpoints = endpoints
	o.upstreams = upstreams
}
func (o *recordingObserver) TableUnchanged()     { o.unchanged++ }
func (o *recordingObserver) TableRefreshFailed() { o.failed++ }

func tableWith(revision string) plane.Table {
	return plane.Table{
		Revision: revision,
		Endpoints: []plane.Endpoint{{
			ID:              "ep_chat",
			Name:            "chat",
			RoutingStrategy: "weighted",
			Upstreams: []plane.Upstream{
				upstream("ap", "dep_1", "http://ap.internal:8000", 100, "running"),
				upstream("us", "dep_2", "http://us.internal:8000", 100, "provisioning"),
			},
		}},
	}
}

// TestTheRouteCarriesTheEndpointUid pins the label a failure rate is joined on.
// Labelling by the public model name instead would split one endpoint's series
// in two the first time somebody renamed it, and the break would look like a
// traffic drop instead of a rename.
func TestTheRouteCarriesTheEndpointUid(t *testing.T) {
	router := New(&stubSource{tables: []plane.Table{tableWith("1")}}, factory())
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}

	route, err := router.Resolve("chat")
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if route.ID != "ep_chat" {
		t.Fatalf("route id = %q, want the endpoint uid the plane sent", route.ID)
	}
}

func TestTheObserverSeesWhatEachRefreshDid(t *testing.T) {
	observer := &recordingObserver{}
	source := &stubSource{
		tables: []plane.Table{tableWith("1")},
		errs:   []error{nil, nil, errors.New("the plane is gone")},
	}
	router := New(source, factory(), WithObserver(observer))
	ctx := context.Background()

	if err := router.Load(ctx); err != nil {
		t.Fatalf("first load: %v", err)
	}
	if observer.applied != 1 || observer.endpoints != 1 {
		t.Fatalf("applied = %d, endpoints = %d, want 1 and 1", observer.applied, observer.endpoints)
	}
	if len(observer.upstreams) != 2 {
		t.Fatalf("upstreams = %v, want both addresses behind the endpoint", observer.upstreams)
	}
	ready := map[string]bool{}
	for _, sample := range observer.upstreams {
		if sample.Endpoint != "ep_chat" || sample.EndpointName != "chat" {
			t.Fatalf("sample = %+v, want it labelled by the endpoint", sample)
		}
		ready[sample.Deployment] = sample.Ready
	}
	if !ready["dep_1"] || ready["dep_2"] {
		t.Fatalf("readiness = %v, want only the running deployment up", ready)
	}

	// The plane answering 304 is a confirmation, not a non-event: it is what
	// says the table in memory is still the right one.
	if err := router.Load(ctx); err != nil {
		t.Fatalf("second load: %v", err)
	}
	if observer.unchanged != 1 {
		t.Fatalf("unchanged = %d, want 1", observer.unchanged)
	}

	// And a plane that cannot be reached is reported without the table moving,
	// because a control-plane outage must not become a data-plane outage.
	if err := router.Load(ctx); err == nil {
		t.Fatal("a failed refresh must still return its error")
	}
	if observer.failed != 1 {
		t.Fatalf("failed = %d, want 1", observer.failed)
	}
	if _, err := router.Resolve("chat"); err != nil {
		t.Fatalf("the loaded table stopped serving after a failed refresh: %v", err)
	}
}

func TestARouterWithNoObserverBehavesIdentically(t *testing.T) {
	router := New(&stubSource{tables: []plane.Table{tableWith("1")}}, factory())
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	if err := router.Load(context.Background()); err != nil {
		t.Fatalf("second load: %v", err)
	}
}
