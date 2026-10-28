package fake

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/api"
)

// The fake is only worth having if it lies about nothing. These tests compare
// what it answers with a corpus captured from a running plane: a fixture that
// disagrees with the server is worse than no fixture at all, because it makes
// the client's tests pass while the client is broken.

func TestSeedMatchesServerShape(t *testing.T) {
	b := newBackend(t)
	ctx := context.Background()

	check(t, "clusters.json", func() (any, error) { return b.Clusters(ctx, api.ListOpts{}) })
	check(t, "nodes.json", func() (any, error) { return b.Nodes(ctx, api.ListOpts{}) })
	check(t, "devices.json", func() (any, error) { return b.Devices(ctx, api.ListOpts{}) })
	check(t, "topology-domains.json", func() (any, error) { return b.TopologyDomains(ctx, api.ListOpts{}) })
	check(t, "pools.json", func() (any, error) { return b.Pools(ctx, api.ListOpts{}) })
	check(t, "artifacts.json", func() (any, error) { return b.Artifacts(ctx, api.ListOpts{}) })
	check(t, "workloads.json", func() (any, error) { return b.Workloads(ctx, api.ListOpts{}) })
	check(t, "deployments.json", func() (any, error) { return b.Deployments(ctx, api.ListOpts{}) })
	check(t, "discovered.json", func() (any, error) { return b.DiscoveredWorkloads(ctx, api.ListOpts{}) })
	check(t, "endpoints.json", func() (any, error) { return b.Endpoints(ctx, api.ListOpts{}) })
	check(t, "projects.json", func() (any, error) { return b.Projects(ctx, api.ListOpts{}) })
	check(t, "quotas.json", func() (any, error) { return b.Quotas(ctx, api.ListOpts{}) })
	check(t, "priority-classes.json", func() (any, error) { return b.PriorityClasses(ctx, api.ListOpts{}) })
	check(t, "policies.json", func() (any, error) { return b.Policies(ctx, api.ListOpts{}) })
	check(t, "allocations.json", func() (any, error) { return b.Allocations(ctx, api.ListOpts{}) })
	check(t, "operations.json", func() (any, error) { return b.Operations(ctx, api.ListOpts{}) })
	check(t, "components.json", func() (any, error) { return b.Components(ctx, api.ListOpts{}) })
	check(t, "events.json", func() (any, error) { return b.Events(ctx, api.ListOpts{}) })
}

func check(t *testing.T, fixture string, call func() (any, error)) {
	t.Helper()
	page, err := call()
	if err != nil {
		t.Fatalf("%s: %v", fixture, err)
	}
	encoded, err := json.Marshal(page)
	if err != nil {
		t.Fatalf("%s: %v", fixture, err)
	}
	want := firstItem(t, readServerFixture(t, fixture))
	got := firstItem(t, encoded)
	for _, problem := range diff("", want, got) {
		t.Errorf("%s: %s", fixture, problem)
	}
}

func firstItem(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	var envelope struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		t.Fatal(err)
	}
	if len(envelope.Items) == 0 {
		t.Fatal("no items to compare against")
	}
	return envelope.Items[0]
}

// freeform names the fields whose contents are arbitrary JSON, not
// schema. Their keys are data, so comparing them would assert that the fake
// holds the same labels as one particular database, which is not the point.
var freeform = map[string]bool{
	"labels": true, "metadata": true, "capabilities": true, "engine_args": true,
	"parameters": true, "config": true, "rules": true, "node_shape": true,
	"attributes": true, "details": true, "overrides": true, "result": true,
	"prefix_cache_config": true, "cluster_floors": true, "live_digests": true,
}

func diff(path string, want, got any) []string {
	if freeform[lastSegment(path)] {
		return nil
	}
	wantMap, wantOK := want.(map[string]any)
	gotMap, gotOK := got.(map[string]any)
	if wantOK && gotOK {
		var out []string
		for _, k := range keysOf(wantMap) {
			if _, ok := gotMap[k]; !ok {
				out = append(out, "fake omits "+at(path, k))
				continue
			}
			out = append(out, diff(at(path, k), wantMap[k], gotMap[k])...)
		}
		for _, k := range keysOf(gotMap) {
			if _, ok := wantMap[k]; !ok {
				out = append(out, "fake invents "+at(path, k))
			}
		}
		return out
	}
	wantList, a := want.([]any)
	gotList, b := got.([]any)
	if a && b && len(wantList) > 0 && len(gotList) > 0 {
		return diff(path+"[0]", wantList[0], gotList[0])
	}
	return nil
}

func keysOf(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func at(path, key string) string {
	if path == "" {
		return key
	}
	return path + "." + key
}

func readServerFixture(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "api", "testdata", name))
	if err != nil {
		t.Fatalf("read %s: %v", name, err)
	}
	return raw
}

func newBackend(t *testing.T) *Backend {
	t.Helper()
	b, err := New("")
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return b
}

func lastSegment(path string) string {
	if i := strings.LastIndex(path, "."); i >= 0 {
		return path[i+1:]
	}
	return path
}
