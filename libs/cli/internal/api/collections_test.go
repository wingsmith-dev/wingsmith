package api

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sort"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
)

// collection is one noun's two entry points, reduced to the thing worth testing about them: which
// path they hit. Each wrapper is two lines, and the failure mode of a wrong one is silent — the
// CLI asks for the wrong collection and prints an empty listing rather than an error.
type collection struct {
	path string
	list func(context.Context, *Client, ListOpts) error
	get  func(context.Context, *Client, string) error
}

func collections() []collection {
	return []collection{
		{"/v1/clusters",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Clusters(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Cluster(c, r); return e }},
		{"/v1/nodes",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Nodes(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Node(c, r); return e }},
		{"/v1/devices",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Devices(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Device(c, r); return e }},
		{"/v1/device-classes",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.DeviceClasses(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.DeviceClass(c, r); return e }},
		{"/v1/topology-domains",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.TopologyDomains(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.TopologyDomain(c, r); return e }},
		{"/v1/pools",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Pools(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Pool(c, r); return e }},
		{"/v1/artifacts",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Artifacts(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Artifact(c, r); return e }},
		{"/v1/workloads",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Workloads(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Workload(c, r); return e }},
		{"/v1/deployments",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Deployments(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Deployment(c, r); return e }},
		{"/v1/discovered",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.DiscoveredWorkloads(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.DiscoveredWorkload(c, r); return e }},
		{"/v1/endpoints",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Endpoints(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Endpoint(c, r); return e }},
		{"/v1/tenants",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Tenants(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Tenant(c, r); return e }},
		{"/v1/projects",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Projects(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Project(c, r); return e }},
		{"/v1/quotas",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Quotas(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Quota(c, r); return e }},
		{"/v1/priority-classes",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.PriorityClasses(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.PriorityClass(c, r); return e }},
		{"/v1/policies",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Policies(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Policy(c, r); return e }},
		{"/v1/allocations",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Allocations(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Allocation(c, r); return e }},
		{"/v1/operations",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Operations(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Operation(c, r); return e }},
		{"/v1/benchmarks",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Benchmarks(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Benchmark(c, r); return e }},
		{"/v1/components",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Components(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Component(c, r); return e }},
		{"/v1/events",
			func(c context.Context, cl *Client, o ListOpts) error { _, e := cl.Events(c, o); return e },
			func(c context.Context, cl *Client, r string) error { _, e := cl.Event(c, r); return e }},
	}
}

func spyClient(t *testing.T, seen *[]*http.Request, body string) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*seen = append(*seen, r.Clone(r.Context()))
		fmt.Fprint(w, body)
	}))
	t.Cleanup(srv.Close)
	c, err := NewClient(srv.URL, "t", "prj_1", "1.0", 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

// TestEveryCollectionListsFromItsOwnPath. A wrapper pointed at the wrong collection answers with
// somebody else's objects, which decode to zero fields and print as an empty table.
func TestEveryCollectionListsFromItsOwnPath(t *testing.T) {
	for _, col := range collections() {
		var seen []*http.Request
		c := spyClient(t, &seen, `{"items":[],"count":0,"next_cursor":""}`)
		if err := col.list(context.Background(), c, ListOpts{}); err != nil {
			t.Errorf("list %s: %v", col.path, err)
			continue
		}
		if len(seen) != 1 {
			t.Errorf("list %s made %d requests", col.path, len(seen))
			continue
		}
		if seen[0].URL.Path != col.path {
			t.Errorf("list %s hit %q", col.path, seen[0].URL.Path)
		}
		if seen[0].Method != http.MethodGet {
			t.Errorf("list %s used %s", col.path, seen[0].Method)
		}
	}
}

// TestEveryCollectionGetsByReferenceUnderItsOwnPath.
func TestEveryCollectionGetsByReferenceUnderItsOwnPath(t *testing.T) {
	for _, col := range collections() {
		var seen []*http.Request
		c := spyClient(t, &seen, `{}`)
		if err := col.get(context.Background(), c, "ref-1"); err != nil {
			t.Errorf("get %s: %v", col.path, err)
			continue
		}
		if len(seen) != 1 {
			t.Errorf("get %s made %d requests", col.path, len(seen))
			continue
		}
		if want := col.path + "/ref-1"; seen[0].URL.Path != want {
			t.Errorf("get %s hit %q, want %q", col.path, seen[0].URL.Path, want)
		}
	}
}

// TestEveryCollectionInTheClientIsCovered holds this table to the client. A new collection added
// without a row here would go untested, and this is the cheapest place to notice.
func TestEveryCollectionInTheClientIsCovered(t *testing.T) {
	covered := map[string]bool{}
	for _, col := range collections() {
		covered[col.path] = true
	}
	// Every path the client can reach through the generic list/get helpers.
	declared := []string{
		"/v1/allocations", "/v1/artifacts", "/v1/benchmarks", "/v1/clusters", "/v1/components",
		"/v1/deployments", "/v1/device-classes", "/v1/devices", "/v1/discovered", "/v1/endpoints",
		"/v1/events", "/v1/nodes", "/v1/operations", "/v1/policies", "/v1/pools",
		"/v1/priority-classes", "/v1/projects", "/v1/quotas", "/v1/tenants",
		"/v1/topology-domains", "/v1/workloads",
	}
	sort.Strings(declared)
	for _, path := range declared {
		if !covered[path] {
			t.Errorf("%s is a collection on the client with no row in this table", path)
		}
	}
	if len(covered) != len(declared) {
		t.Errorf("the table covers %d collections, the client has %d", len(covered), len(declared))
	}
}

// TestPaginationIsOfferedByEveryCollection — a wrapper that dropped ListOpts would silently
// ignore --limit on one noun and honour it on every other.
func TestPaginationIsOfferedByEveryCollection(t *testing.T) {
	for _, col := range collections() {
		var seen []*http.Request
		c := spyClient(t, &seen, `{"items":[],"count":0,"next_cursor":""}`)
		if err := col.list(context.Background(), c, ListOpts{Limit: 7, Cursor: "abc"}); err != nil {
			t.Errorf("list %s: %v", col.path, err)
			continue
		}
		q := seen[0].URL.Query()
		if q.Get("limit") != "7" {
			t.Errorf("%s dropped --limit: %q", col.path, seen[0].URL.RawQuery)
		}
		if q.Get("cursor") != "abc" {
			t.Errorf("%s dropped the cursor: %q", col.path, seen[0].URL.RawQuery)
		}
	}
}

// TestAnErrorFromAnyCollectionIsTyped — every path has to produce the same error contract, or a
// script branching on the code works for some nouns and not others.
func TestAnErrorFromAnyCollectionIsTyped(t *testing.T) {
	for _, col := range collections() {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			fmt.Fprint(w, `{"code":"not_found","message":"nope"}`)
		}))
		c, _ := NewClient(srv.URL, "", "", "1.0", time.Second)

		listErr := col.list(context.Background(), c, ListOpts{})
		getErr := col.get(context.Background(), c, "ref-1")
		srv.Close()

		for what, err := range map[string]error{"list": listErr, "get": getErr} {
			if err == nil {
				t.Errorf("%s %s accepted a 404", what, col.path)
				continue
			}
			if e := errorCode(err); e != "NOT_FOUND" {
				t.Errorf("%s %s gave code %q, want NOT_FOUND", what, col.path, e)
			}
		}
	}
}

func errorCode(err error) string {
	return string(errs.As(err).Code)
}
