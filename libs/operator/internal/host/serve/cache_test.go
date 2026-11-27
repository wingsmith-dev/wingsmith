package serve

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// cacheRig is a rig whose backend prefetches, with an empty cache and a settable clock and disk.
type cacheRig struct {
	*rig
	cache *Cache
	creds string
	now   time.Time
	used  uint64
}

func newCacheRig(t *testing.T) *cacheRig {
	t.Helper()
	r := newRig(t)
	if err := os.RemoveAll(filepath.Join(r.models, "hf")); err != nil {
		t.Fatal(err)
	}
	cr := &cacheRig{rig: r, creds: t.TempDir(), now: time.Date(2026, 11, 27, 0, 0, 0, 0, time.UTC), used: 10}
	cr.cache = &Cache{
		Docker: r.b.Docker, Policy: r.b.Policy, Root: r.models, CredentialsDir: cr.creds, Log: r.b.Log,
		Now:   func() time.Time { return cr.now },
		Usage: func(string) (uint64, uint64, error) { return cr.used, 100, nil },
	}
	r.b.Cache = cr.cache
	return cr
}

func (cr *cacheRig) prefetches() []*fakeContainer {
	var out []*fakeContainer
	for _, c := range cr.docker.containers {
		if c.spec.Labels[LabelManagedBy] == prefetchManagedBy {
			out = append(out, c)
		}
	}
	return out
}

// finish ends a prefetch container, after writing what a download would have written.
func (cr *cacheRig) finish(t *testing.T, id string, exit int, files ...string) {
	t.Helper()
	for _, f := range files {
		p := filepath.Join(cr.models, filepath.FromSlash(f))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("weights"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	for _, c := range cr.prefetches() {
		if c.spec.Labels[LabelArtifact] == id {
			c.running, c.exitCode = false, exit
			return
		}
	}
	t.Fatalf("no prefetch container for %s", id)
}

func lastCache(rec *recorder, id string) *wire.CacheObserved {
	for i := len(rec.frames) - 1; i >= 0; i-- {
		if p, ok := rec.frames[i].payload.(wire.CacheObserved); ok && p.ArtifactID == id {
			return &p
		}
	}
	return nil
}

func warm(ids ...string) wire.CacheDesired {
	out := wire.CacheDesired{}
	for _, id := range ids {
		out.Entries = append(out.Entries, wire.CacheWarmEntry{ArtifactID: id})
	}
	return out
}

func TestPrefetchLifecycleAndResidency(t *testing.T) {
	cr := newCacheRig(t)
	rec := &recorder{}
	d := NewDriver(&Driver{Backend: cr.b, Reporter: rec, ClusterID: "clu_1", Log: cr.b.Log})
	ctx := context.Background()
	_ = d.Handle(ctx, envelope(t, typeArtifactDesired, artifact()))
	_ = d.Handle(ctx, envelope(t, typeCacheDesired, warm("art_1")))
	_ = d.Handle(ctx, envelope(t, typeDeploymentDesired, deployment()))
	d.Pass(ctx)

	if got := rec.last(typeDeploymentObserved); got.Reason != "WeightsNotResident" {
		t.Fatalf("deployment while fetching: %+v", got)
	}
	if got := lastCache(rec, "art_1"); got == nil || got.State != "fetching" || got.ClusterID != "clu_1" {
		t.Fatalf("cache while fetching: %+v", got)
	}
	pf := cr.prefetches()
	if len(pf) != 1 {
		t.Fatalf("prefetch containers = %d", len(pf))
	}
	spec := pf[0].spec
	if spec.Image != image || !slices.Equal(spec.Entrypoint, []string{"python3", "-c"}) ||
		!strings.Contains(spec.Cmd[0], "snapshot_download") || !strings.Contains(spec.Cmd[0], `"org/model", "rev1"`) {
		t.Fatalf("spec = %+v", spec)
	}
	repo := "hf/hub/models--org--model"
	locks := "hf/hub/.locks/models--org--model"
	want := []string{filepath.Join(cr.models, repo) + ":/models/" + repo, filepath.Join(cr.models, locks) + ":/models/" + locks}
	if !slices.Equal(spec.HostConfig.Binds, want) {
		t.Fatalf("binds = %v, want only the repo and its locks read-write: %v", spec.HostConfig.Binds, want)
	}
	if !slices.Contains(spec.Env, "HF_HOME=/models/hf") || !spec.HostConfig.ReadonlyRootfs ||
		!slices.Equal(spec.HostConfig.CapDrop, []string{"ALL"}) || spec.HostConfig.RestartPolicy.Name != "no" {
		t.Fatalf("hardening = %+v env %v", spec.HostConfig, spec.Env)
	}
	if want := fmt.Sprintf("%d:%d", os.Getuid(), os.Getgid()); spec.User != want {
		t.Fatalf("prefetch runs as %q, want the agent's own %q", spec.User, want)
	}
	for _, e := range spec.Env {
		if strings.HasPrefix(e, "HF_TOKEN=") {
			t.Fatal("public artifact got a token with no _default.token")
		}
	}

	// A non-empty directory is not resident without the marker: it could be half a download.
	cr.finish(t, "art_1", 0, "hf/hub/models--org--model/snapshots/rev1/config.json")
	cr.prefetches()[0].running = true
	d.Pass(ctx)
	if got := rec.last(typeDeploymentObserved); got.Reason != "WeightsNotResident" {
		t.Fatalf("served a running download: %+v", got)
	}

	cr.prefetches()[0].running = false
	d.Pass(ctx)
	got := lastCache(rec, "art_1")
	if got.State != "resident" || !got.Resident || got.BytesResident == 0 || got.EvictionPressure != 0.1 {
		t.Fatalf("cache after finish: %+v", got)
	}
	if len(cr.prefetches()) != 0 {
		t.Fatal("finished prefetch container was not removed")
	}
	if _, err := os.Stat(filepath.Join(cr.models, ".wingsmith/artifacts/art_1.json")); err != nil {
		t.Fatalf("marker: %v", err)
	}
	if dep := rec.last(typeDeploymentObserved); dep.Phase != "running" {
		t.Fatalf("deployment after residency: %+v", dep)
	}
	for _, c := range cr.docker.containers {
		if c.spec.Labels[LabelModelPath] != "hf/hub/models--org--model/snapshots/rev1" {
			t.Fatalf("replica lacks the model-path label: %v", c.spec.Labels)
		}
	}
	// No new prefetch once resident, and no repeated report.
	n := len(rec.frames)
	d.Pass(ctx)
	if len(cr.prefetches()) != 0 {
		t.Fatal("refetched a resident artifact")
	}
	for _, f := range rec.frames[n:] {
		if f.typ == typeCacheObserved {
			t.Fatalf("repeated cache report %+v", f.payload)
		}
	}
}

func TestPrefetchFailureBacksOff(t *testing.T) {
	cr := newCacheRig(t)
	ctx := context.Background()
	in := SyncInput{Warm: warm("art_1"), Artifacts: map[string]wire.ArtifactDesired{"art_1": artifact()},
		Deployments: []wire.DeploymentDesired{deployment()}}
	cr.cache.Sync(ctx, in)
	cr.finish(t, "art_1", 1)
	out := cr.cache.Sync(ctx, in)
	if len(out) != 1 || out[0].Reason != "PrefetchFailed" || !strings.Contains(out[0].Message, "exited with 1") {
		t.Fatalf("= %+v", out)
	}
	if len(cr.prefetches()) != 0 {
		t.Fatal("retried inside the backoff")
	}
	cr.now = cr.now.Add(2 * time.Minute)
	cr.cache.Sync(ctx, in)
	if len(cr.prefetches()) != 1 {
		t.Fatal("did not retry after the backoff")
	}
}

func privateArtifact(ref bool) wire.ArtifactDesired {
	a := wire.ArtifactDesired{ArtifactID: "art_p", OwnerProject: "prj_1",
		Sources: []wire.ArtifactSource{{Kind: "hf", URI: "hf://acme/secret@rev2"}}}
	if ref {
		s := "hf-prj_1"
		a.Sources[0].CredentialRef = &s
	}
	return a
}

func TestPrivateArtifactTokenResolution(t *testing.T) {
	cr := newCacheRig(t)
	ctx := context.Background()
	dep := deployment()
	dep.ArtifactIDs = []string{"art_p"}
	in := SyncInput{Warm: warm("art_p"), Artifacts: map[string]wire.ArtifactDesired{"art_p": privateArtifact(true)},
		Deployments: []wire.DeploymentDesired{dep}}

	out := cr.cache.Sync(ctx, in)
	if len(out) != 1 || out[0].Reason != "CredentialUnavailable" || !strings.Contains(out[0].Message, "prj_1.token") {
		t.Fatalf("missing token: %+v", out)
	}
	if len(cr.prefetches()) != 0 {
		t.Fatal("fetched a private artifact without its token")
	}

	tokenFile := filepath.Join(cr.creds, "prj_1.token")
	if err := os.WriteFile(tokenFile, []byte("hf_secret\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	out = cr.cache.Sync(ctx, in)
	if len(out) != 1 || out[0].Reason != "CredentialUnavailable" || !strings.Contains(out[0].Message, "chmod 600") {
		t.Fatalf("loose token file: %+v", out)
	}

	// A symlink is refused even when its target is a proper token file.
	real := filepath.Join(t.TempDir(), "real.token")
	if err := os.WriteFile(real, []byte("hf_secret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(tokenFile); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(real, tokenFile); err != nil {
		t.Fatal(err)
	}
	out = cr.cache.Sync(ctx, in)
	if len(out) != 1 || out[0].Reason != "CredentialUnavailable" || !strings.Contains(out[0].Message, "symlink") {
		t.Fatalf("symlinked token file: %+v", out)
	}
	if err := os.Remove(tokenFile); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tokenFile, []byte("hf_secret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cr.cache.Sync(ctx, in)
	pf := cr.prefetches()
	if len(pf) != 1 {
		t.Fatalf("prefetches = %d", len(pf))
	}
	spec := pf[0].spec
	if !slices.Contains(spec.Env, "HF_TOKEN=hf_secret") || !slices.Contains(spec.Env, "HF_HOME=/models/p/prj_1/hf") {
		t.Fatalf("env = %v", spec.Env)
	}
	repo, locks := "p/prj_1/hf/hub/models--acme--secret", "p/prj_1/hf/hub/.locks/models--acme--secret"
	if want := []string{filepath.Join(cr.models, repo) + ":/models/" + repo, filepath.Join(cr.models, locks) + ":/models/" + locks}; !slices.Equal(spec.HostConfig.Binds, want) {
		t.Fatalf("binds = %v", spec.HostConfig.Binds)
	}

	// A private artifact naming no credential fetches without one, as on a cluster.
	cr2 := newCacheRig(t)
	in.Artifacts["art_p"] = privateArtifact(false)
	cr2.cache.Sync(ctx, in)
	if pf := cr2.prefetches(); len(pf) != 1 || slices.ContainsFunc(pf[0].spec.Env, func(e string) bool { return strings.HasPrefix(e, "HF_TOKEN=") }) {
		t.Fatalf("no-credential private artifact: %+v", pf)
	}
}

func TestPrefetchRefusals(t *testing.T) {
	cr := newCacheRig(t)
	ctx := context.Background()
	bad := wire.ArtifactDesired{ArtifactID: "art_x", OwnerProject: "../etc", Sources: []wire.ArtifactSource{{URI: "hf://a/b@c"}}}
	out := cr.cache.Sync(ctx, SyncInput{Warm: warm("art_x", "art_1", "art_gone"),
		Artifacts: map[string]wire.ArtifactDesired{"art_x": bad, "art_1": artifact()}})
	reasons := map[string]string{}
	for _, o := range out {
		reasons[o.ArtifactID] = o.Reason
	}
	if reasons["art_x"] != "InvalidArtifact" || reasons["art_1"] != "NoRuntimeImage" || reasons["art_gone"] != "ArtifactNotDeclared" {
		t.Fatalf("reasons = %v", reasons)
	}
	cr.cache.Image = "docker.io/evil/fetch:latest"
	out = cr.cache.Sync(ctx, SyncInput{Warm: warm("art_1"), Artifacts: map[string]wire.ArtifactDesired{"art_1": artifact()}})
	if len(out) != 1 || out[0].Reason != "ImageNotAllowed" || len(cr.prefetches()) != 0 {
		t.Fatalf("= %+v", out)
	}
}

func urlArtifact(id string) wire.ArtifactDesired {
	return wire.ArtifactDesired{ArtifactID: id, Sources: []wire.ArtifactSource{{Kind: "url", URI: "https://example.com/" + id + ".bin"}}}
}

func TestConcurrentPrefetchLimit(t *testing.T) {
	cr := newCacheRig(t)
	cr.cache.MaxConcurrent = 1
	arts := map[string]wire.ArtifactDesired{"art_a": urlArtifact("art_a"), "art_b": urlArtifact("art_b")}
	cr.cache.Image = image
	out := cr.cache.Sync(context.Background(), SyncInput{Warm: warm("art_a", "art_b"), Artifacts: arts})
	if len(cr.prefetches()) != 1 {
		t.Fatalf("prefetches = %d", len(cr.prefetches()))
	}
	pending := 0
	for _, o := range out {
		if o.Reason == "PrefetchPending" {
			pending++
		}
	}
	if pending != 1 {
		t.Fatalf("= %+v", out)
	}
}

func TestRestartCollectsAndCleansPartials(t *testing.T) {
	cr := newCacheRig(t)
	ctx := context.Background()
	cr.cache.Image = image
	arts := map[string]wire.ArtifactDesired{"art_a": urlArtifact("art_a"), "art_b": urlArtifact("art_b")}
	in := SyncInput{Warm: warm("art_a", "art_b"), Artifacts: arts}
	cr.cache.Sync(ctx, in)
	// The host goes down: art_a's download finishes meanwhile, art_b's dies half-written.
	cr.finish(t, "art_a", 0, "art_a/art_a.bin")
	cr.finish(t, "art_b", 137, "art_b/art_b.bin")

	restarted := &Cache{Docker: cr.cache.Docker, Policy: cr.cache.Policy, Root: cr.models, CredentialsDir: cr.creds,
		Image: image, Log: cr.b.Log, Now: cr.cache.Now, Usage: cr.cache.Usage}
	if restarted.Resident(arts["art_b"]) {
		t.Fatal("a half-written directory read as resident")
	}
	restarted.Sync(ctx, in)
	if !restarted.Resident(arts["art_a"]) {
		t.Fatal("a download that finished while the host was down was not collected")
	}
	// art_b failed and backs off; past it, the retry starts from an empty directory.
	cr.now = cr.now.Add(2 * time.Minute)
	restarted.Sync(ctx, in)
	if _, err := os.Stat(filepath.Join(cr.models, "art_b", "art_b.bin")); !os.IsNotExist(err) {
		t.Fatalf("partial download kept: %v", err)
	}
	if pf := cr.prefetches(); len(pf) != 1 || pf[0].spec.Labels[LabelArtifact] != "art_b" {
		t.Fatalf("retry = %+v", pf)
	}
}

// A cache filled by hand (or from before markers) and mounted by a running replica is adopted on
// upgrade, never deleted and fetched over.
func TestHandFilledCacheIsAdopted(t *testing.T) {
	cr := newCacheRig(t)
	ctx := context.Background()
	cr.cache.Image = image
	weights := filepath.Join(cr.models, "art_h", "model.safetensors")
	if err := os.MkdirAll(filepath.Dir(weights), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(weights, []byte("hand-filled"), 0o644); err != nil {
		t.Fatal(err)
	}
	cr.docker.containers["r1"] = &fakeContainer{id: "r1", name: "ws-x-0", running: true, spec: ContainerCreate{
		Image: image, Labels: map[string]string{LabelManagedBy: managedBy, LabelModelPath: "art_h"}}}
	arts := map[string]wire.ArtifactDesired{"art_h": urlArtifact("art_h")}

	out := cr.cache.Sync(ctx, SyncInput{Warm: warm("art_h"), Artifacts: arts})
	if len(out) != 1 || out[0].Reason != "Resident" {
		t.Fatalf("= %+v", out)
	}
	if len(cr.prefetches()) != 0 {
		t.Fatal("fetched over a hand-filled cache")
	}
	if raw, err := os.ReadFile(weights); err != nil || string(raw) != "hand-filled" {
		t.Fatalf("hand-filled weights touched: %q %v", raw, err)
	}
	if !cr.cache.Resident(arts["art_h"]) {
		t.Fatal("adopted weights not resident")
	}
}

func TestEvictionKeepsWarmAndMounted(t *testing.T) {
	cr := newCacheRig(t)
	ctx := context.Background()
	cr.cache.Image = image
	ids := []string{"art_old", "art_warm", "art_mounted", "art_new"}
	arts := map[string]wire.ArtifactDesired{}
	for _, id := range ids {
		arts[id] = urlArtifact(id)
	}
	for i, id := range ids {
		cr.now = cr.now.Add(time.Hour)
		cr.cache.Sync(ctx, SyncInput{Warm: warm(id), Artifacts: arts})
		cr.finish(t, id, 0, id+"/"+id+".bin")
		cr.cache.Sync(ctx, SyncInput{Warm: warm(id), Artifacts: arts})
		if !cr.cache.Resident(arts[id]) {
			t.Fatalf("%d %s not resident", i, id)
		}
	}
	// A replica mounting art_mounted, the oldest after art_old.
	cr.docker.images[image] = true
	cr.docker.containers["r1"] = &fakeContainer{id: "r1", name: "ws-x-0", running: true, spec: ContainerCreate{
		Image: image, Labels: map[string]string{LabelManagedBy: managedBy, LabelModelPath: "art_mounted"}}}

	// Below the high watermark nothing goes.
	cr.used = 85
	if out := cr.cache.Sync(ctx, SyncInput{Warm: warm("art_warm"), Artifacts: arts}); len(out) != 0 {
		t.Fatalf("evicted below the watermark: %+v", out)
	}
	// Usage follows the markers on disk, 10% each: from 95 it takes two evictions to reach 80.
	base := uint64(55)
	cr.cache.Usage = func(string) (uint64, uint64, error) { return base + 10*uint64(len(cr.cache.markers())), 100, nil }
	out := cr.cache.Sync(ctx, SyncInput{Warm: warm("art_warm"), Artifacts: arts})
	var evicted []string
	for _, o := range out {
		if o.Reason == "Evicted" {
			evicted = append(evicted, o.ArtifactID)
		}
	}
	if !slices.Equal(evicted, []string{"art_old", "art_new"}) {
		t.Fatalf("evicted %v, want the least recently used outside the warm set and not mounted", evicted)
	}
	for _, id := range []string{"art_warm", "art_mounted"} {
		if !cr.cache.Resident(arts[id]) {
			t.Fatalf("%s was evicted", id)
		}
	}
	if _, err := os.Stat(filepath.Join(cr.models, "art_old")); !os.IsNotExist(err) {
		t.Fatal("evicted weights still on disk")
	}

	// A stopped replica does not hold its weights: art_mounted goes once its replica stops.
	cr.docker.containers["r1"].running = false
	base = 80
	out = cr.cache.Sync(ctx, SyncInput{Warm: warm("art_warm"), Artifacts: arts})
	if len(out) != 1 || out[0].ArtifactID != "art_mounted" || out[0].Reason != "Evicted" {
		t.Fatalf("stopped replica's weights: %+v", out)
	}
	cr.docker.containers["r1"].running = true

	// A running replica without the model-path label stops eviction outright; a stopped one does not.
	cr.docker.containers["r2"] = &fakeContainer{id: "r2", name: "ws-y-0", running: true, spec: ContainerCreate{
		Image: image, Labels: map[string]string{LabelManagedBy: managedBy}}}
	base = 80
	if out := cr.cache.Sync(ctx, SyncInput{Artifacts: arts}); len(out) != 0 {
		t.Fatalf("evicted beside an unlabelled replica: %+v", out)
	}
}
