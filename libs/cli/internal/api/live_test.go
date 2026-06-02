package api

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
)

// These run against a real plane when one is pointed at, and are skipped
// otherwise. The corpus in testdata keeps the types honest without a server;
// this keeps the transport honest with one — query params, envelopes, the
// error body, and cursor following are things only a real server can confirm.
//
//	HANGAR_TEST_SERVER=http://localhost:8099 go test ./internal/api/
func liveClient(t *testing.T) *Client {
	t.Helper()
	server := os.Getenv("HANGAR_TEST_SERVER")
	if server == "" {
		t.Skip("set HANGAR_TEST_SERVER to run against a live plane")
	}
	c, err := NewClient(server, os.Getenv("HANGAR_TEST_TOKEN"), "", "test", 20*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestLiveEveryCollectionAnswers(t *testing.T) {
	c := liveClient(t)
	ctx := context.Background()

	calls := map[string]func() (int, error){
		"clusters":         func() (int, error) { p, err := c.Clusters(ctx, ListOpts{}); return p.Count, err },
		"nodes":            func() (int, error) { p, err := c.Nodes(ctx, ListOpts{}); return p.Count, err },
		"devices":          func() (int, error) { p, err := c.Devices(ctx, ListOpts{}); return p.Count, err },
		"topology-domains": func() (int, error) { p, err := c.TopologyDomains(ctx, ListOpts{}); return p.Count, err },
		"pools":            func() (int, error) { p, err := c.Pools(ctx, ListOpts{}); return p.Count, err },
		"artifacts":        func() (int, error) { p, err := c.Artifacts(ctx, ListOpts{}); return p.Count, err },
		"workloads":        func() (int, error) { p, err := c.Workloads(ctx, ListOpts{}); return p.Count, err },
		"deployments":      func() (int, error) { p, err := c.Deployments(ctx, ListOpts{}); return p.Count, err },
		"endpoints":        func() (int, error) { p, err := c.Endpoints(ctx, ListOpts{}); return p.Count, err },
		"projects":         func() (int, error) { p, err := c.Projects(ctx, ListOpts{}); return p.Count, err },
		"quotas":           func() (int, error) { p, err := c.Quotas(ctx, ListOpts{}); return p.Count, err },
		"priority-classes": func() (int, error) { p, err := c.PriorityClasses(ctx, ListOpts{}); return p.Count, err },
		"policies":         func() (int, error) { p, err := c.Policies(ctx, ListOpts{}); return p.Count, err },
		"allocations":      func() (int, error) { p, err := c.Allocations(ctx, ListOpts{}); return p.Count, err },
		"operations":       func() (int, error) { p, err := c.Operations(ctx, ListOpts{}); return p.Count, err },
		"components":       func() (int, error) { p, err := c.Components(ctx, ListOpts{}); return p.Count, err },
		"events":           func() (int, error) { p, err := c.Events(ctx, ListOpts{}); return p.Count, err },
	}
	for name, call := range calls {
		if _, err := call(); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestLiveCursorFollowing(t *testing.T) {
	c := liveClient(t)
	ctx := context.Background()

	first, err := c.Devices(ctx, ListOpts{Limit: 2})
	if err != nil {
		t.Fatal(err)
	}
	if first.NextCursor == "" {
		t.Skip("the live corpus fits in one page")
	}
	second, err := c.Devices(ctx, ListOpts{Limit: 2, Cursor: first.NextCursor})
	if err != nil {
		t.Fatal(err)
	}
	if len(second.Items) > 0 && second.Items[0].ID == first.Items[0].ID {
		t.Error("the cursor returned the same page")
	}
	all, err := c.Devices(ctx, ListOpts{Limit: 2, All: true})
	if err != nil {
		t.Fatal(err)
	}
	if all.NextCursor != "" || all.Count <= first.Count {
		t.Errorf("following cursors stopped early: count=%d cursor=%q", all.Count, all.NextCursor)
	}
}

func TestLiveFiltersAreUnderstood(t *testing.T) {
	c := liveClient(t)
	ctx := context.Background()

	clusters, err := c.Clusters(ctx, ListOpts{All: true})
	if err != nil || len(clusters.Items) == 0 {
		t.Skipf("no clusters to filter by (%v)", err)
	}
	name := clusters.Items[0].Name

	nodes, err := c.Nodes(ctx, ListOpts{Cluster: name, All: true})
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range nodes.Items {
		if n.ClusterName != name {
			t.Fatalf("?cluster=%s returned a node from %s", name, n.ClusterName)
		}
	}

	// A name, not just an id: libs/packages/contract/API.md §3 says a path segment accepts
	// either, and the CLI relies on it everywhere.
	one, err := c.Cluster(ctx, name)
	if err != nil {
		t.Fatalf("resolving a cluster by name: %v", err)
	}
	byID, err := c.Cluster(ctx, one.ID)
	if err != nil || byID.Name != name {
		t.Fatalf("resolving the same cluster by id: %v", err)
	}
}

func TestLiveErrorsCarryACode(t *testing.T) {
	c := liveClient(t)
	_, err := c.Cluster(context.Background(), "no-such-cluster-anywhere")
	if err == nil {
		t.Fatal("a missing cluster did not error")
	}
	if got := errs.As(err).Code; got != errs.CodeNotFound {
		t.Errorf("missing cluster produced %q, want NOT_FOUND", got)
	}
}
