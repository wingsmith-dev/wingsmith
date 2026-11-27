package serve

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

func (r *rig) gpus(uuids ...string) {
	r.b.GPUs = func(context.Context) (string, []string, error) { return "box", uuids, nil }
}

func (r *rig) generation(t *testing.T, name string) string {
	t.Helper()
	for _, c := range r.docker.containers {
		if c.name == name {
			return c.spec.Labels[LabelGeneration]
		}
	}
	t.Fatalf("no container %s in %v", name, r.names())
	return ""
}

func TestRolloutSurgesWhenCardsAreFree(t *testing.T) {
	r := newRig(t)
	r.gpus("GPU-a", "GPU-b", "GPU-c", "GPU-d")
	ctx := context.Background()
	if _, err := r.b.Apply(ctx, desired(deployment())); err != nil {
		t.Fatal(err)
	}
	if _, err := r.b.Observe(ctx, deployment()); err != nil {
		t.Fatal(err)
	}
	old := r.b.Ready()["dep_1"]

	dep := deployment()
	dep.Generation = 2
	r.healthy.Store(false)
	if _, err := r.b.Apply(ctx, desired(dep)); err != nil {
		t.Fatal(err)
	}
	want := []string{"ws-qwen-chat-0", "ws-qwen-chat-0-g2", "ws-qwen-chat-1", "ws-qwen-chat-1-g2"}
	if got := r.names(); !slices.Equal(got, want) {
		t.Fatalf("surge = %v, want %v", got, want)
	}
	held := map[string]bool{}
	for _, name := range want {
		g, ok := r.b.Allocations.Get(name)
		if !ok || len(g.GPUs) != 1 || held[g.GPUs[0]] {
			t.Fatalf("%s grant %v, held %v", name, g, held)
		}
		held[g.GPUs[0]] = true
	}

	// Not ready yet: nothing is stopped, and the old replicas stay routed (probe down: none).
	if _, err := r.b.Observe(ctx, dep); err != nil {
		t.Fatal(err)
	}
	if _, err := r.b.Apply(ctx, desired(dep)); err != nil {
		t.Fatal(err)
	}
	if len(r.names()) != 4 {
		t.Fatalf("old stopped before the new was ready: %v", r.names())
	}

	// Ready: the route switches, then the next apply stops the old and frees its cards.
	r.healthy.Store(true)
	if _, err := r.b.Observe(ctx, dep); err != nil {
		t.Fatal(err)
	}
	if len(r.b.Ready()["dep_1"]) != len(old) || len(r.b.routed["dep_1"]) != 2 || !r.b.routed["dep_1"]["ws-qwen-chat-0-g2"] {
		t.Fatalf("routes = %v %v", r.b.Ready(), r.b.routed)
	}
	if _, err := r.b.Apply(ctx, desired(dep)); err != nil {
		t.Fatal(err)
	}
	if got := r.names(); !slices.Equal(got, []string{"ws-qwen-chat-0-g2", "ws-qwen-chat-1-g2"}) {
		t.Fatalf("after the switch = %v", got)
	}
	if _, ok := r.b.Allocations.Get("ws-qwen-chat-0"); ok {
		t.Fatal("the stopped replica still holds its card")
	}
	// The next generation takes the plain names back.
	dep.Generation = 3
	_, _ = r.b.Apply(ctx, desired(dep))
	if got := r.names(); !slices.Contains(got, "ws-qwen-chat-0") || r.generation(t, "ws-qwen-chat-0") != "3" {
		t.Fatalf("generation 3 = %v", got)
	}
}

func TestRolloutRoutesOldUntilNewIsReady(t *testing.T) {
	r := newRig(t)
	ctx := context.Background()
	dep := deployment()
	dep.MinReplicas = 1
	if _, err := r.b.Apply(ctx, desired(dep)); err != nil {
		t.Fatal(err)
	}
	dep.Generation = 2
	if _, err := r.b.Apply(ctx, desired(dep)); err != nil {
		t.Fatal(err)
	}
	// The new replica is not running yet, so the old one stays routed.
	for _, c := range r.docker.containers {
		if c.spec.Labels[LabelGeneration] == "2" {
			c.running = false
		}
	}
	if _, err := r.b.Observe(ctx, dep); err != nil {
		t.Fatal(err)
	}
	if !r.b.isRouted("dep_1", "ws-qwen-chat-0") || len(r.b.Ready()["dep_1"]) != 1 {
		t.Fatalf("old replica not routed during the rollout: %v %v", r.b.routed, r.b.Ready())
	}
	if _, err := r.b.Apply(ctx, desired(dep)); err != nil {
		t.Fatal(err)
	}
	if len(r.names()) != 2 {
		t.Fatalf("old removed while the new was unready: %v", r.names())
	}
}

func TestRolloutStopsFirstWithoutFreeCards(t *testing.T) {
	r := newRig(t)
	r.gpus("GPU-a", "GPU-b")
	ctx := context.Background()
	if _, err := r.b.Apply(ctx, desired(deployment())); err != nil {
		t.Fatal(err)
	}
	dep := deployment()
	dep.Generation = 2
	if _, err := r.b.Apply(ctx, desired(dep)); err != nil {
		t.Fatal(err)
	}
	if got := r.names(); !slices.Equal(got, []string{"ws-qwen-chat-0", "ws-qwen-chat-1"}) {
		t.Fatalf("stop-first = %v", got)
	}
	for _, name := range r.names() {
		if r.generation(t, name) != "2" {
			t.Fatalf("%s not replaced", name)
		}
	}
	g0, _ := r.b.Allocations.Get("ws-qwen-chat-0")
	g1, _ := r.b.Allocations.Get("ws-qwen-chat-1")
	if len(g0.GPUs) != 1 || len(g1.GPUs) != 1 || g0.GPUs[0] == g1.GPUs[0] {
		t.Fatalf("grants %v %v", g0, g1)
	}
}

func TestRestartAdoptsAndDropsStaleGrants(t *testing.T) {
	r := newRig(t)
	ctx := context.Background()
	if _, err := r.b.Apply(ctx, desired(deployment())); err != nil {
		t.Fatal(err)
	}
	g0, _ := r.b.Allocations.Get("ws-qwen-chat-0")
	g1, _ := r.b.Allocations.Get("ws-qwen-chat-1")
	// The host dies holding a grant for a container that never got created, and one container is
	// removed behind its back.
	if _, err := r.b.Allocations.Allocate("ws-ghost-0", "dep_x", 1, []string{"GPU-a", "GPU-b", "GPU-c"}); err != nil {
		t.Fatal(err)
	}
	for id, c := range r.docker.containers {
		if c.name == "ws-qwen-chat-1" {
			delete(r.docker.containers, id)
		}
	}
	_ = r.b.Allocations.Close()
	reopened, err := OpenAllocations(r.state)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	r.b.Allocations = reopened
	if err := r.b.Adopt(ctx); err != nil {
		t.Fatal(err)
	}
	if got, ok := reopened.Get("ws-qwen-chat-0"); !ok || !slices.Equal(got.GPUs, g0.GPUs) {
		t.Fatalf("adopted grant = %v, want %v", got, g0)
	}
	for _, gone := range []string{"ws-ghost-0", "ws-qwen-chat-1"} {
		if _, ok := reopened.Get(gone); ok {
			t.Fatalf("stale grant %s kept", gone)
		}
	}
	// Re-creating the missing replica never hands out the adopted card.
	if _, err := r.b.Apply(ctx, desired(deployment())); err != nil {
		t.Fatal(err)
	}
	g1b, _ := reopened.Get("ws-qwen-chat-1")
	if len(g1b.GPUs) != 1 || g1b.GPUs[0] == g0.GPUs[0] {
		t.Fatalf("recreated replica got %v, adopted holds %v (was %v)", g1b, g0, g1)
	}
	raw, _ := os.ReadFile(r.state)
	var onDisk map[string]Grant
	if err := json.Unmarshal(raw, &onDisk); err != nil || len(onDisk) != 2 {
		t.Fatalf("persisted map = %s %v", raw, err)
	}
}

func TestWriteDurableReplaces(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	for _, body := range []string{`{"a":1}`, `{"b":2}`} {
		if err := writeDurable(path, []byte(body)); err != nil {
			t.Fatal(err)
		}
		if got, _ := os.ReadFile(path); string(got) != body {
			t.Fatalf("= %s, want %s", got, body)
		}
	}
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatalf("temporary file left: %v", err)
	}
}

func TestDriverPausedStopsAndKeepsDesired(t *testing.T) {
	r := newRig(t)
	rec := &recorder{}
	d := NewDriver(&Driver{Backend: r.b, Reporter: rec, ClusterID: "clu_1", Log: r.b.Log})
	ctx := context.Background()
	_ = d.Handle(ctx, envelope(t, typeArtifactDesired, artifact()))
	_ = d.Handle(ctx, envelope(t, typeDeploymentDesired, deployment()))
	d.Pass(ctx)
	if len(r.docker.containers) != 2 {
		t.Fatalf("not serving: %v", r.names())
	}
	dep := deployment()
	dep.Generation, dep.Lifecycle = 2, "paused"
	_ = d.Handle(ctx, envelope(t, typeDeploymentDesired, dep))
	d.Pass(ctx)
	if len(r.docker.containers) != 0 {
		t.Fatalf("paused deployment still runs %v", r.names())
	}
	for _, name := range []string{"ws-qwen-chat-0", "ws-qwen-chat-1"} {
		if _, ok := r.b.Allocations.Get(name); ok {
			t.Fatalf("paused replica %s holds its card", name)
		}
	}
	if len(d.deps) != 1 {
		t.Fatal("pause forgot the declaration")
	}
	dep.Generation, dep.Lifecycle = 3, wire.DeploymentDesired{}.Lifecycle
	_ = d.Handle(ctx, envelope(t, typeDeploymentDesired, dep))
	d.Pass(ctx)
	if len(r.docker.containers) != 2 {
		t.Fatalf("resume = %v", r.names())
	}
}
