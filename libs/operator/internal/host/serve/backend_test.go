package serve

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/backend"
	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

const image = "ghcr.io/acme/vllm@sha256:0123456789abcdef"

type rig struct {
	b       *Backend
	docker  *fakeDocker
	healthy *atomic.Bool
	state   string
	models  string
	port    int
}

func newRig(t *testing.T) *rig {
	t.Helper()
	healthy := &atomic.Bool{}
	healthy.Store(true)
	probe := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/ready" || !healthy.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
	}))
	t.Cleanup(probe.Close)
	u, _ := url.Parse(probe.URL)
	docker, client := newFakeDocker(t, u.Port())
	docker.images[image] = true
	dir := t.TempDir()
	models := filepath.Join(dir, "models")
	snap := filepath.Join(models, "hf/hub/models--org--model/snapshots/rev1")
	if err := os.MkdirAll(snap, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(snap, "config.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	state := filepath.Join(dir, "state", "allocations.json")
	alloc, err := OpenAllocations(state)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = alloc.Close() })
	port, _ := strconv.Atoi(u.Port())
	return &rig{
		b: &Backend{
			Docker: client, Allocations: alloc, ClusterID: "clu_1", ModelRoot: models,
			Policy: Policy{ImagePrefixes: []string{"ghcr.io/acme"}},
			GPUs: func(context.Context) (string, []string, error) {
				return "box", []string{"GPU-a", "GPU-b", "GPU-c"}, nil
			},
			Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		},
		docker: docker, healthy: healthy, state: state, models: models, port: port,
	}
}

func deployment() wire.DeploymentDesired {
	return wire.DeploymentDesired{
		DeploymentID: "dep_1", Name: "Qwen Chat", ProjectID: "prj_1", MinReplicas: 2, Generation: 1,
		ArtifactIDs:     []string{"art_1"},
		ServedModelName: "qwen",
		Placement:       wire.PlacementConstraints{DevicesPerReplica: 1},
		Serving: wire.ServingSpec{Runtime: wire.RuntimeContainer, Digest: "svg_1", Container: &wire.ContainerSpec{
			Image: image, Args: []string{"--model", "$(WS_MODEL_PATH)", "--port", "$(WS_PORT)"},
			Env:            []wire.EnvVar{{Name: "EXTRA", Value: "1"}},
			Ports:          []wire.ContainerPort{{Name: "http", ContainerPort: 8000}},
			ReadinessProbe: map[string]any{"httpGet": map[string]any{"path": "/ready", "port": "http"}},
		}},
	}
}

func artifact() wire.ArtifactDesired {
	return wire.ArtifactDesired{ArtifactID: "art_1", Sources: []wire.ArtifactSource{{Kind: "hf", URI: "hf://org/model@rev1"}}}
}

func desired(dep wire.DeploymentDesired) backend.Desired {
	return backend.Desired{Deployment: dep, Artifact: artifact(), Layout: backend.Layout{DevicesPerNode: 1, NodesPerReplica: 1}}
}

func (r *rig) names() []string {
	var out []string
	for _, c := range r.docker.containers {
		out = append(out, c.name)
	}
	slices.Sort(out)
	return out
}

func TestApplyCreatesHardenedReplicas(t *testing.T) {
	r := newRig(t)
	ctx := context.Background()
	applied, err := r.b.Apply(ctx, desired(deployment()))
	if err != nil {
		t.Fatal(err)
	}
	if applied.Name != "qwen-chat" || applied.Unchanged {
		t.Fatalf("applied = %+v", applied)
	}
	if got := r.names(); !slices.Equal(got, []string{"ws-qwen-chat-0", "ws-qwen-chat-1"}) {
		t.Fatalf("containers = %v", got)
	}
	gpus := map[string]bool{}
	for _, spec := range r.docker.creates {
		hc := spec.HostConfig
		if !slices.Equal(hc.SecurityOpt, []string{"no-new-privileges"}) || !slices.Equal(hc.CapDrop, []string{"ALL"}) ||
			!hc.ReadonlyRootfs || hc.Privileged || hc.Tmpfs["/tmp"] == "" {
			t.Fatalf("security options missing: %+v", hc)
		}
		if want := filepath.Join(r.models, "hf") + ":/models/hf:ro"; !slices.Equal(hc.Binds, []string{want}) {
			t.Fatalf("binds = %v, want %s", hc.Binds, want)
		}
		if b := hc.PortBindings["8000/tcp"]; len(b) != 1 || b[0].HostIP != "127.0.0.1" || b[0].HostPort != "" {
			t.Fatalf("port not on ephemeral loopback: %+v", hc.PortBindings)
		}
		if len(hc.DeviceRequests) != 1 || len(hc.DeviceRequests[0].DeviceIDs) != 1 || hc.DeviceRequests[0].Driver != "nvidia" {
			t.Fatalf("device request = %+v", hc.DeviceRequests)
		}
		gpus[hc.DeviceRequests[0].DeviceIDs[0]] = true
		if !slices.Contains(spec.Env, "WS_MODEL_PATH=/models/hf/hub/models--org--model/snapshots/rev1") ||
			!slices.Contains(spec.Env, "WS_SERVED_MODEL_NAME=qwen") || !slices.Contains(spec.Env, "EXTRA=1") {
			t.Fatalf("env = %v", spec.Env)
		}
		if strings.Join(spec.Cmd, " ") != "--model /models/hf/hub/models--org--model/snapshots/rev1 --port 8000" {
			t.Fatalf("args not expanded: %v", spec.Cmd)
		}
		if spec.Labels[LabelDeployment] != "dep_1" || spec.Labels[LabelGeneration] != "1" || spec.Labels[LabelManagedBy] != "wingsmith-host" {
			t.Fatalf("labels = %v", spec.Labels)
		}
	}
	if len(gpus) != 2 {
		t.Fatalf("replicas share a GPU: %v", gpus)
	}

	again, err := r.b.Apply(ctx, desired(deployment()))
	if err != nil || !again.Unchanged || len(r.docker.creates) != 2 {
		t.Fatalf("re-apply of the same inputs wrote: %+v %v creates=%d", again, err, len(r.docker.creates))
	}
}

func TestApplyCDIDevices(t *testing.T) {
	r := newRig(t)
	r.b.CDI = true
	if _, err := r.b.Apply(context.Background(), desired(deployment())); err != nil {
		t.Fatal(err)
	}
	dr := r.docker.creates[0].HostConfig.DeviceRequests[0]
	if dr.Driver != "cdi" || !strings.HasPrefix(dr.DeviceIDs[0], "nvidia.com/gpu=GPU-") {
		t.Fatalf("cdi request = %+v", dr)
	}
}

func TestGPUAssignmentPersistsAndAdopts(t *testing.T) {
	r := newRig(t)
	ctx := context.Background()
	if _, err := r.b.Apply(ctx, desired(deployment())); err != nil {
		t.Fatal(err)
	}
	g0, _ := r.b.Allocations.Get("ws-qwen-chat-0")
	g1, _ := r.b.Allocations.Get("ws-qwen-chat-1")

	// A second process cannot take the map while this one holds it.
	if _, err := OpenAllocations(r.state); err == nil {
		t.Fatal("a second open of the allocation map succeeded")
	}
	_ = r.b.Allocations.Close()
	reopened, err := OpenAllocations(r.state)
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := reopened.Get("ws-qwen-chat-0"); !slices.Equal(got.GPUs, g0.GPUs) {
		t.Fatalf("persisted grant = %v, want %v", got.GPUs, g0.GPUs)
	}
	_ = reopened.Close()

	// The file is lost; adoption rebuilds it from the containers' labels.
	_ = os.Remove(r.state)
	fresh, err := OpenAllocations(r.state)
	if err != nil {
		t.Fatal(err)
	}
	r.b.Allocations = fresh
	if err := r.b.Adopt(ctx); err != nil {
		t.Fatal(err)
	}
	if got, _ := fresh.Get("ws-qwen-chat-1"); !slices.Equal(got.GPUs, g1.GPUs) {
		t.Fatalf("adopted grant = %v, want %v", got.GPUs, g1.GPUs)
	}
	// A third replica gets the one card nobody holds.
	dep := deployment()
	dep.MinReplicas = 3
	if _, err := r.b.Apply(ctx, desired(dep)); err != nil {
		t.Fatal(err)
	}
	g2, _ := fresh.Get("ws-qwen-chat-2")
	if len(g2.GPUs) != 1 || g2.GPUs[0] == g0.GPUs[0] || g2.GPUs[0] == g1.GPUs[0] {
		t.Fatalf("third replica got %v, held %v %v", g2.GPUs, g0.GPUs, g1.GPUs)
	}
	dep.MinReplicas = 4
	if _, err := r.b.Apply(ctx, desired(dep)); !errors.Is(err, ErrNoCapacity) {
		t.Fatalf("fourth replica on three cards: %v, want ErrNoCapacity", err)
	}
}

func TestRolloutReplacesPrunesAndDeletes(t *testing.T) {
	r := newRig(t)
	ctx := context.Background()
	if _, err := r.b.Apply(ctx, desired(deployment())); err != nil {
		t.Fatal(err)
	}
	dep := deployment()
	dep.Generation, dep.MinReplicas = 2, 1
	applied, err := r.b.Apply(ctx, desired(dep))
	if err != nil || applied.Unchanged {
		t.Fatalf("generation bump: %+v %v", applied, err)
	}
	if err := r.b.Prune(ctx, dep, applied.Name); err != nil {
		t.Fatal(err)
	}
	// A free card lets the new replica start beside the old one, which goes once it is routed.
	if got := r.names(); !slices.Equal(got, []string{"ws-qwen-chat-0", "ws-qwen-chat-0-g2"}) {
		t.Fatalf("after scale-down = %v", got)
	}
	if _, err := r.b.Observe(ctx, dep); err != nil {
		t.Fatal(err)
	}
	if _, err := r.b.Apply(ctx, desired(dep)); err != nil {
		t.Fatal(err)
	}
	if got := r.names(); !slices.Equal(got, []string{"ws-qwen-chat-0-g2"}) {
		t.Fatalf("after the switch = %v", got)
	}
	for _, c := range r.docker.containers {
		if c.spec.Labels[LabelGeneration] != "2" {
			t.Fatalf("stale replica kept: %v", c.spec.Labels)
		}
	}
	if _, ok := r.b.Allocations.Get("ws-qwen-chat-1"); ok {
		t.Fatal("pruned replica still holds its GPU")
	}
	// A rename leaves the old name's containers to Prune.
	dep.Name = "renamed"
	applied, _ = r.b.Apply(ctx, desired(dep))
	_ = r.b.Prune(ctx, dep, applied.Name)
	if got := r.names(); !slices.Equal(got, []string{"ws-renamed-0"}) {
		t.Fatalf("after rename = %v", got)
	}
	if err := r.b.Delete(ctx, "dep_1"); err != nil {
		t.Fatal(err)
	}
	if len(r.docker.containers) != 0 {
		t.Fatalf("delete left %v", r.names())
	}
	if err := r.b.Delete(ctx, "dep_1"); err != nil {
		t.Fatalf("delete of nothing: %v", err)
	}
}

func TestObserveRunsTheDeclaredProbe(t *testing.T) {
	r := newRig(t)
	ctx := context.Background()
	if _, err := r.b.Apply(ctx, desired(deployment())); err != nil {
		t.Fatal(err)
	}
	out, err := r.b.Observe(ctx, deployment())
	if err != nil {
		t.Fatal(err)
	}
	if out.Phase != "running" || out.Reason != "Serving" || out.Ready != 2 || len(out.Replicas) != 2 || len(out.Allocations) != 2 {
		t.Fatalf("healthy rollout = %+v", out)
	}
	if ports := r.b.Ready()["dep_1"]; len(ports) != 2 || ports[0] != r.port {
		t.Fatalf("ready ports = %v, want the published loopback port %d", ports, r.port)
	}

	r.healthy.Store(false)
	out, _ = r.b.Observe(ctx, deployment())
	if out.Phase != "placed" || out.Reason != "Applied" || out.Ready != 0 || out.Replicas[0].Phase != "starting" {
		t.Fatalf("unready rollout = %+v", out)
	}
	if len(r.b.Ready()["dep_1"]) != 0 {
		t.Fatal("unready replicas still routed")
	}

	for _, c := range r.docker.containers {
		c.running, c.restarts, c.exitCode = false, 5, 1
	}
	out, _ = r.b.Observe(ctx, deployment())
	if out.Phase != "failed" || out.Reason != "CrashLooping" || out.LastError == nil {
		t.Fatalf("crash-looping rollout = %+v", out)
	}
}

func TestApplyRefusals(t *testing.T) {
	cases := map[string]struct {
		mutate func(*rig, *backend.Desired)
		want   string
	}{
		"no allowlist fails closed": {func(r *rig, _ *backend.Desired) { r.b.Policy.ImagePrefixes = nil }, "allowed image prefixes"},
		"tag without digest":        {func(_ *rig, d *backend.Desired) { d.Deployment.Serving.Container.Image = "ghcr.io/acme/vllm:latest" }, "pinned by digest"},
		"outside the allowlist": {func(_ *rig, d *backend.Desired) {
			d.Deployment.Serving.Container.Image = "ghcr.io/acme-fork/vllm@sha256:00"
		}, "outside the allowed prefixes"},
		"dynamo runtime":      {func(_ *rig, d *backend.Desired) { d.Deployment.Serving.Runtime = "dynamo" }, "runtime: container only"},
		"invalid artifact id": {func(_ *rig, d *backend.Desired) { d.Artifact = wire.ArtifactDesired{ArtifactID: "../etc"} }, "not a valid id"},
		"traversal in hf ref": {func(_ *rig, d *backend.Desired) { d.Artifact.Sources[0].URI = "hf://../x@rev" }, "not a valid Hugging Face"},
		"exec probe": {func(_ *rig, d *backend.Desired) {
			d.Deployment.Serving.Container.ReadinessProbe = map[string]any{"exec": map[string]any{"command": []string{"true"}}}
		}, "exec probes"},
		"multi-node": {func(_ *rig, d *backend.Desired) { d.Layout.NodesPerReplica = 2 }, "aggregated"},
		"env overrides WS_PORT": {func(_ *rig, d *backend.Desired) {
			d.Deployment.Serving.Container.Env = []wire.EnvVar{{Name: "WS_PORT", Value: "1"}}
		}, "set by the platform"},
		"env in the WS_ namespace": {func(_ *rig, d *backend.Desired) {
			d.Deployment.Serving.Container.Env = []wire.EnvVar{{Name: "WS_ANYTHING", Value: "1"}}
		}, "set by the platform"},
		"env name with =": {func(_ *rig, d *backend.Desired) {
			d.Deployment.Serving.Container.Env = []wire.EnvVar{{Name: "A=B", Value: "1"}}
		}, "not a valid name"},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			r := newRig(t)
			d := desired(deployment())
			d.Deployment.Serving.Container = &wire.ContainerSpec{}
			*d.Deployment.Serving.Container = *deployment().Serving.Container
			tc.mutate(r, &d)
			_, err := r.b.Apply(context.Background(), d)
			var rejected *backend.Rejected
			if !errors.As(err, &rejected) || !strings.Contains(rejected.Message, tc.want) {
				t.Fatalf("err = %v, want a rejection mentioning %q", err, tc.want)
			}
			if len(r.docker.containers) != 0 {
				t.Fatal("a refused deployment created containers")
			}
		})
	}
}

func TestTagsAllowedWhenConfigured(t *testing.T) {
	r := newRig(t)
	r.b.Policy.TagsAllowed = true
	d := desired(deployment())
	d.Deployment.Serving.Container.Image = "ghcr.io/acme/vllm:1.0"
	r.docker.images["ghcr.io/acme/vllm:1.0"] = true
	if _, err := r.b.Apply(context.Background(), d); err != nil {
		t.Fatal(err)
	}
}

func TestWeightsNotCached(t *testing.T) {
	r := newRig(t)
	_ = os.RemoveAll(filepath.Join(r.models, "hf"))
	if _, err := r.b.Apply(context.Background(), desired(deployment())); !errors.Is(err, ErrWeightsNotCached) {
		t.Fatalf("err = %v, want ErrWeightsNotCached", err)
	}
}

func TestPullsAMissingImage(t *testing.T) {
	r := newRig(t)
	delete(r.docker.images, image)
	if _, err := r.b.Apply(context.Background(), desired(deployment())); err != nil {
		t.Fatal(err)
	}
	r2 := newRig(t)
	delete(r2.docker.images, image)
	r2.docker.pullErr = "denied"
	_, err := r2.b.Apply(context.Background(), desired(deployment()))
	var pull *pullError
	if !errors.As(err, &pull) {
		t.Fatalf("err = %v, want a pull error", err)
	}
	if _, ok := r2.b.Allocations.Get("ws-qwen-chat-0"); ok {
		t.Fatal("a failed pull kept its GPU")
	}
}

type recorder struct{ frames []sent }

type sent struct {
	typ, subject string
	payload      any
}

func (r *recorder) Send(t syncpkg.MessageType, subject string, payload any) error {
	r.frames = append(r.frames, sent{t, subject, payload})
	return nil
}

func (r *recorder) last(typ string) *wire.DeploymentObserved {
	for i := len(r.frames) - 1; i >= 0; i-- {
		if r.frames[i].typ == typ {
			p := r.frames[i].payload.(wire.DeploymentObserved)
			return &p
		}
	}
	return nil
}

func envelope(t *testing.T, typ string, payload any) *syncpkg.Envelope {
	t.Helper()
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	return &syncpkg.Envelope{Type: typ, Payload: raw}
}

func TestDriverServesReportsAndTearsDown(t *testing.T) {
	r := newRig(t)
	rec := &recorder{}
	var routes map[string][]int
	state := filepath.Join(t.TempDir(), "desired.json")
	d := NewDriver(&Driver{Backend: r.b, Reporter: rec, ClusterID: "clu_1", StatePath: state,
		Routes: func(m map[string][]int) { routes = m }, Log: r.b.Log})
	ctx := context.Background()

	_ = d.Handle(ctx, envelope(t, typeDeploymentDesired, deployment()))
	d.Pass(ctx)
	if got := rec.last(typeDeploymentObserved); got == nil || got.Reason != "ArtifactNotDeclared" {
		t.Fatalf("before the artifact: %+v", got)
	}
	_ = d.Handle(ctx, envelope(t, typeArtifactDesired, artifact()))
	d.Pass(ctx)
	if got := rec.last(typeDeploymentObserved); got.Phase != "running" || got.ReadyReplicas != 2 {
		t.Fatalf("after apply: %+v", got)
	}
	if len(routes["dep_1"]) != 2 {
		t.Fatalf("routes = %v", routes)
	}

	// A restarted host keeps what it was told.
	d2 := NewDriver(&Driver{Backend: r.b, Reporter: rec, ClusterID: "clu_1", StatePath: state, Log: r.b.Log})
	if err := d2.load(); err != nil || len(d2.deps) != 1 || len(d2.artifacts) != 1 {
		t.Fatalf("checkpoint reload: %v %d %d", err, len(d2.deps), len(d2.artifacts))
	}

	_ = d.Handle(ctx, envelope(t, typeDeploymentDeleted, wire.DeploymentDeleted{DeploymentID: "dep_1"}))
	d.Pass(ctx)
	if got := rec.last(typeDeploymentObserved); got.Phase != "terminating" || got.ReplicaIDs == nil || len(got.ReplicaIDs) != 0 {
		t.Fatalf("after delete: %+v", got)
	}
	if len(r.docker.containers) != 0 || len(routes) != 0 {
		t.Fatalf("teardown left containers %v routes %v", r.names(), routes)
	}
	released := 0
	for _, f := range rec.frames {
		if a, ok := f.payload.(wire.AllocationObserved); ok && len(a.DeviceIDs) == 0 {
			released++
		}
	}
	if released != 2 {
		t.Fatalf("released %d allocations, want 2", released)
	}
}

func TestDriverReportsWeightsNotResident(t *testing.T) {
	r := newRig(t)
	_ = os.RemoveAll(filepath.Join(r.models, "hf"))
	rec := &recorder{}
	d := NewDriver(&Driver{Backend: r.b, Reporter: rec, ClusterID: "clu_1", Log: r.b.Log})
	ctx := context.Background()
	_ = d.Handle(ctx, envelope(t, typeArtifactDesired, artifact()))
	_ = d.Handle(ctx, envelope(t, typeDeploymentDesired, deployment()))
	d.Pass(ctx)
	got := rec.last(typeDeploymentObserved)
	if got.Phase != "pending" || got.Reason != "WeightsNotResident" || !strings.Contains(got.Message, "not cached") {
		t.Fatalf("= %+v", got)
	}
}
