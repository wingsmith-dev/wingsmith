package serve

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/fetchscript"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// The host model cache (P2.5, ADR 0035). Weights are fetched by a short-lived container running
// the engine image with only the artifact's cache subtree mounted read-write, and an artifact is
// resident only once its completion marker exists. Markers live in markerDir under the cache root,
// which no container ever mounts, so a container cannot declare its own download complete.

// Defaults for the cache settings in host.json.
const (
	DefaultCredentialsDir = "/etc/wingsmith/credentials"
	DefaultMaxPrefetches  = 2
	DefaultHighWatermark  = 0.90
	DefaultLowWatermark   = 0.80

	// LabelArtifact names the artifact a prefetch container fetches.
	LabelArtifact = "wingsmith.dev/artifact"
	// LabelModelPath is the cache path a replica container serves, relative to the cache root.
	// Eviction reads it to leave mounted weights alone.
	LabelModelPath = "wingsmith.dev/model-path"

	prefetchManagedBy = "wingsmith-host-prefetch"
	markerDir         = ".wingsmith/artifacts"
	// publicTokenFile is the optional token public artifacts are fetched with. Project ids cannot
	// start with "_", so it never collides with a project's file.
	publicTokenFile = "_default.token"
)

const (
	retryBase = time.Minute
	retryMax  = 30 * time.Minute
)

// Cache keeps the host's model cache: prefetch, residency and eviction.
type Cache struct {
	Docker Docker
	Policy Policy
	// Root is the cache directory; DefaultModelRoot when empty.
	Root string
	// CredentialsDir holds <project>.token files an admin drops; DefaultCredentialsDir when empty.
	CredentialsDir string
	// Image fetches every artifact when set. Otherwise the image of a deployment serving it.
	Image string
	// MaxConcurrent bounds the prefetch containers running at once.
	MaxConcurrent int
	// HighWatermark and LowWatermark are fractions of the filesystem in use: eviction starts above
	// the first and stops at the second.
	HighWatermark, LowWatermark float64
	// Usage reports the filesystem holding path; statfs when nil.
	Usage func(path string) (used, total uint64, err error)
	Log   *slog.Logger
	Now   func() time.Time

	mu       sync.Mutex
	failures map[string]*failure
	reported map[string]string
}

type failure struct {
	attempts int
	next     time.Time
	message  string
}

// marker is the completion record of one artifact.
type marker struct {
	ArtifactID  string    `json:"artifact_id"`
	Path        string    `json:"path"`
	Bytes       int64     `json:"bytes"`
	CompletedAt time.Time `json:"completed_at"`
	LastUsedAt  time.Time `json:"last_used_at"`
}

// target is where one artifact lands and how it is fetched.
type target struct {
	art   wire.ArtifactDesired
	model model
	// size is the directory measured for bytes: the repo dir for Hugging Face (blobs included),
	// the artifact's directory otherwise.
	size string
	// script is the python the container runs.
	script string
	// hf is set for Hugging Face artifacts, whose download resumes instead of starting over.
	hf bool
}

func (c *Cache) root() string {
	if c.Root == "" {
		return DefaultModelRoot
	}
	return c.Root
}

func (c *Cache) log() *slog.Logger {
	if c.Log == nil {
		return slog.Default()
	}
	return c.Log
}

func (c *Cache) now() time.Time {
	if c.Now == nil {
		return time.Now().UTC()
	}
	return c.Now().UTC()
}

func (c *Cache) host(rel string) string { return filepath.Join(c.root(), filepath.FromSlash(rel)) }

// targetFor validates the artifact with the serving rules (modelFor) and picks its source.
func targetFor(art wire.ArtifactDesired) (target, error) {
	m, err := modelFor(art)
	if err != nil {
		return target{}, err
	}
	_, other := pickSources(art)
	// The same source modelFor resolved the path from: the first Hugging Face one.
	var hf *wire.ArtifactSource
	for i := range art.Sources {
		if strings.HasPrefix(art.Sources[i].URI, "hf://") {
			hf = &art.Sources[i]
			break
		}
	}
	if hf != nil {
		repo, rev, _ := strings.Cut(strings.TrimPrefix(hf.URI, "hf://"), "@")
		repoDir := m.path[:strings.Index(m.path, "/snapshots/")]
		return target{art: art, model: m, size: repoDir, script: fetchscript.HF(repo, rev), hf: true}, nil
	}
	if other == nil {
		return target{}, reject("artifact %s declares no source to fetch from", art.ArtifactID)
	}
	return target{art: art, model: m, size: m.path, script: fetchscript.URL(other.URI, containerModelRoot+"/"+m.path)}, nil
}

// pickSources is the preferred Hugging Face source and the preferred other one, by priority.
func pickSources(art wire.ArtifactDesired) (hf, other *wire.ArtifactSource) {
	for i := range art.Sources {
		s := &art.Sources[i]
		if s.URI == "" {
			continue
		}
		slot := &other
		if strings.HasPrefix(s.URI, "hf://") {
			slot = &hf
		}
		if *slot == nil || s.Priority < (*slot).Priority {
			*slot = s
		}
	}
	return hf, other
}

func (c *Cache) markerPath(id string) string {
	return filepath.Join(c.root(), filepath.FromSlash(markerDir), id+".json")
}

func (c *Cache) readMarker(id string) (marker, bool) {
	raw, err := os.ReadFile(c.markerPath(id))
	if err != nil {
		return marker{}, false
	}
	var m marker
	if json.Unmarshal(raw, &m) != nil || m.ArtifactID != id {
		return marker{}, false
	}
	return m, true
}

func (c *Cache) writeMarker(m marker) error {
	raw, err := json.Marshal(m)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(c.markerPath(m.ArtifactID)), 0o700); err != nil {
		return err
	}
	return writeDurable(c.markerPath(m.ArtifactID), raw)
}

// Resident says the artifact's download completed and its weights are still on disk.
func (c *Cache) Resident(art wire.ArtifactDesired) bool {
	t, err := targetFor(art)
	if err != nil {
		return false
	}
	m, ok := c.readMarker(art.ArtifactID)
	return ok && m.Path == t.model.path && cached(c.root(), t.model)
}

// token is the Hugging Face token an artifact is fetched with, read from the credentials
// directory. A private artifact that names a credential must find its project's file; the value
// never comes from the plane.
func (c *Cache) token(art wire.ArtifactDesired) (string, error) {
	dir := c.CredentialsDir
	if dir == "" {
		dir = DefaultCredentialsDir
	}
	if art.OwnerProject == "" {
		tok, err := readToken(filepath.Join(dir, publicTokenFile))
		if errors.Is(err, os.ErrNotExist) {
			return "", nil
		}
		return tok, err
	}
	hf, other := pickSources(art)
	src := hf
	if src == nil {
		src = other
	}
	if src == nil || src.CredentialRef == nil || *src.CredentialRef == "" {
		return "", nil
	}
	// modelFor has validated OwnerProject, so it is one path component.
	file := filepath.Join(dir, art.OwnerProject+".token")
	tok, err := readToken(file)
	if errors.Is(err, os.ErrNotExist) {
		return "", fmt.Errorf("artifact %s is private to project %s and needs its token in %s", art.ArtifactID, art.OwnerProject, file)
	}
	return tok, err
}

// SyncInput is the declared state one cache pass works from.
type SyncInput struct {
	// Warm is the cache.desired warm-set.
	Warm wire.CacheDesired
	// Artifacts is every declared artifact, by id.
	Artifacts map[string]wire.ArtifactDesired
	// Deployments is every declared deployment; the artifacts of the ones that run are wanted too.
	Deployments []wire.DeploymentDesired
}

// Sync runs one cache pass: it collects finished downloads, starts the ones wanted and not yet
// resident, evicts under disk pressure, and returns the residency reports that changed.
func (c *Cache) Sync(ctx context.Context, in SyncInput) []wire.CacheObserved {
	c.mu.Lock()
	if c.failures == nil {
		c.failures = map[string]*failure{}
		c.reported = map[string]string{}
	}
	c.mu.Unlock()

	wanted := map[string]bool{}
	images := map[string]string{}
	for _, e := range in.Warm.Entries {
		wanted[e.ArtifactID] = true
	}
	for _, dep := range in.Deployments {
		if dep.Authority == "observed" || dep.Lifecycle == "paused" {
			continue
		}
		for _, id := range dep.ArtifactIDs {
			wanted[id] = true
			if dep.Serving.Container != nil && images[id] == "" {
				images[id] = dep.Serving.Container.Image
			}
		}
	}

	pressure := 0.0
	if used, total, err := c.usage(); err == nil && total > 0 {
		pressure = float64(used) / float64(total)
	}

	var out []wire.CacheObserved
	report := func(id, state, reason, message string, bytes int64) {
		if o, ok := c.observed(id, state, reason, message, bytes, pressure); ok {
			out = append(out, o)
		}
	}

	running, err := c.collect(ctx, wanted, in.Artifacts)
	if err != nil {
		c.log().Warn("could not list prefetch containers", "error", err)
		return nil
	}

	ids := make([]string, 0, len(wanted))
	for id := range wanted {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		art, ok := in.Artifacts[id]
		if !ok {
			report(id, "absent", "ArtifactNotDeclared", fmt.Sprintf("artifact %s has not reached this host yet", id), 0)
			continue
		}
		t, err := targetFor(art)
		if err != nil {
			report(id, "absent", "InvalidArtifact", err.Error(), 0)
			continue
		}
		if m, ok := c.readMarker(id); ok && m.Path == t.model.path && cached(c.root(), t.model) {
			report(id, "resident", "Resident", "the weights are on this host", m.Bytes)
			continue
		}
		if !t.hf && !running[id] && c.adopt(t) {
			m, _ := c.readMarker(id)
			report(id, "resident", "Resident", "the weights are on this host", m.Bytes)
			continue
		}
		if running[id] {
			report(id, "fetching", "Fetching", "the download is running", dirSize(c.host(t.size)))
			continue
		}
		if f := c.failed(id); f != nil && c.now().Before(f.next) {
			report(id, "absent", "PrefetchFailed", f.message, 0)
			continue
		}
		img := c.Image
		if img == "" {
			img = images[id]
		}
		if img == "" {
			report(id, "absent", "NoRuntimeImage", fmt.Sprintf(
				"no deployment on this host serving %s names an image to fetch it with, and no prefetch_image is set", id), 0)
			continue
		}
		if err := c.Policy.checkImage("prefetch "+id, img); err != nil {
			report(id, "absent", "ImageNotAllowed", err.Error(), 0)
			continue
		}
		tok, err := c.token(art)
		if err != nil {
			report(id, "absent", "CredentialUnavailable", err.Error(), 0)
			continue
		}
		if len(running) >= c.maxConcurrent() {
			report(id, "absent", "PrefetchPending", "waiting for a download slot", 0)
			continue
		}
		if err := c.start(ctx, t, img, tok); err != nil {
			c.fail(id, "could not start the download: "+err.Error())
			report(id, "absent", "PrefetchFailed", err.Error(), 0)
			continue
		}
		running[id] = true
		report(id, "fetching", "Fetching", "the download is running", 0)
	}

	for _, id := range c.evict(ctx, wanted) {
		report(id, "absent", "Evicted", "evicted under disk pressure", 0)
	}
	return out
}

func (c *Cache) maxConcurrent() int {
	if c.MaxConcurrent <= 0 {
		return DefaultMaxPrefetches
	}
	return c.MaxConcurrent
}

func (c *Cache) usage() (uint64, uint64, error) {
	if c.Usage != nil {
		return c.Usage(c.root())
	}
	return statfsUsage(c.root())
}

// observed builds a report unless it says what the last one for this artifact said.
func (c *Cache) observed(id, state, reason, message string, bytes int64, pressure float64) (wire.CacheObserved, bool) {
	key := fmt.Sprintf("%s|%s|%s|%d", state, reason, message, bytes)
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.reported[id] == key {
		return wire.CacheObserved{}, false
	}
	c.reported[id] = key
	return wire.CacheObserved{
		ArtifactID: id, State: state, Resident: state == "resident", BytesResident: bytes,
		Nodes: []wire.CacheNodeResidency{}, EvictionPressure: pressure,
		Reason: reason, Message: message, ObservedAt: c.now().Format(time.RFC3339Nano),
	}, true
}

// Forget drops a report so it is sent again, after a send failed.
func (c *Cache) Forget(id string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.reported, id)
}

func (c *Cache) failed(id string) *failure {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.failures[id]
}

// fail records a failed attempt and backs the next one off exponentially. The count lives in
// memory: a restart retries at once, which is a single attempt per restart, not a loop.
func (c *Cache) fail(id, message string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	f := c.failures[id]
	if f == nil {
		f = &failure{}
		c.failures[id] = f
	}
	f.attempts++
	wait := retryBase << min(f.attempts-1, 5)
	f.next = c.now().Add(min(wait, retryMax))
	f.message = fmt.Sprintf("the download failed (attempt %d, next try after %s): %s", f.attempts, f.next.Format(time.RFC3339), message)
}

// collect walks the prefetch containers: a finished one is recorded and removed, one no longer
// wanted is stopped. It returns the artifacts still downloading. This is also what recovers after
// a restart: a container that finished while the host was down is collected here.
func (c *Cache) collect(ctx context.Context, wanted map[string]bool, arts map[string]wire.ArtifactDesired) (map[string]bool, error) {
	list, err := c.Docker.List(ctx, map[string]string{LabelManagedBy: prefetchManagedBy})
	if err != nil {
		return nil, err
	}
	running := map[string]bool{}
	for _, ct := range list {
		id := ct.Labels[LabelArtifact]
		info, err := c.Docker.Inspect(ctx, ct.ID)
		if errors.Is(err, ErrNotFound) {
			continue
		}
		if err != nil {
			return nil, err
		}
		if info.State.Running {
			if wanted[id] {
				running[id] = true
				continue
			}
			c.log().Info("stopping a download no longer wanted", "artifact", id)
			_ = c.Docker.Remove(ctx, ct.ID)
			continue
		}
		art, declared := arts[id]
		t, terr := targetFor(art)
		switch {
		case info.State.ExitCode != 0:
			c.fail(id, fmt.Sprintf("the prefetch container exited with %d", info.State.ExitCode))
		case !declared || terr != nil || ct.Labels[LabelModelPath] != t.model.path:
			// The artifact changed or went away while the container ran: nothing to record.
		case !cached(c.root(), t.model):
			c.fail(id, "the prefetch container exited cleanly but left "+t.model.path+" empty")
		default:
			now := c.now()
			if err := c.writeMarker(marker{ArtifactID: id, Path: t.model.path, Bytes: dirSize(c.host(t.size)),
				CompletedAt: now, LastUsedAt: now}); err != nil {
				c.log().Warn("could not write the completion marker", "artifact", id, "error", err)
				continue
			}
			_ = os.Remove(c.fetchingPath(id))
			c.mu.Lock()
			delete(c.failures, id)
			c.mu.Unlock()
			c.log().Info("weights resident", "artifact", id, "path", t.model.path)
		}
		if err := c.Docker.Remove(ctx, ct.ID); err != nil {
			c.log().Warn("could not remove prefetch container", "artifact", id, "error", err)
		}
	}
	return running, nil
}

func prefetchName(id string) string { return "ws-prefetch-" + id }

// fetchingPath is the breadcrumb a plain download leaves while it runs. It marks what is under the
// artifact's path as the host's own partial download, which a retry may delete; without it the
// contents were put there by someone else (an operator filling the cache by hand) and are adopted.
func (c *Cache) fetchingPath(id string) string {
	return filepath.Join(c.root(), filepath.FromSlash(markerDir), id+".fetching")
}

// adopt records weights already under a plain artifact's path that no download of ours left: a
// cache filled by hand, or one from before markers existed. The check is the cheap one, a
// non-empty directory, plus the declared size when the artifact has one.
func (c *Cache) adopt(t target) bool {
	id := t.art.ArtifactID
	if _, err := os.Stat(c.fetchingPath(id)); err == nil || !cached(c.root(), t.model) {
		return false
	}
	size := dirSize(c.host(t.size))
	if t.art.SizeBytes > 0 && size < t.art.SizeBytes {
		return false
	}
	now := c.now()
	if err := c.writeMarker(marker{ArtifactID: id, Path: t.model.path, Bytes: size, CompletedAt: now, LastUsedAt: now}); err != nil {
		c.log().Warn("could not write the completion marker", "artifact", id, "error", err)
		return false
	}
	c.log().Info("adopted weights already in the cache", "artifact", id, "path", t.model.path)
	return true
}

// replicaPaths is every model path a replica container, in any state, is labelled with. ok is
// false when one has no label, so what it mounts is unknown.
func (c *Cache) replicaPaths(ctx context.Context, live bool) (paths map[string]bool, ok bool, err error) {
	list, err := c.Docker.List(ctx, map[string]string{LabelManagedBy: managedBy})
	if err != nil {
		return nil, false, err
	}
	paths, ok = map[string]bool{}, true
	for _, ct := range list {
		if live && ct.State != "running" && ct.State != "restarting" {
			continue
		}
		p, has := ct.Labels[LabelModelPath]
		if !has {
			ok = false
			continue
		}
		paths[p] = true
	}
	return paths, ok, nil
}

// prefetchBinds are the only host paths a prefetch container gets, all read-write: the Hugging
// Face repo's own directory and its lock directory, or the plain artifact's own directory. Never
// HF_HOME or the project directory, so one download cannot touch another artifact's files.
func (c *Cache) prefetchBinds(t target) []string {
	dirs := prefetchDirs(t)
	out := make([]string, len(dirs))
	for i, d := range dirs {
		out[i] = c.host(d) + ":" + containerModelRoot + "/" + d
	}
	return out
}

func prefetchDirs(t target) []string {
	if !t.hf {
		return []string{t.model.path}
	}
	hub, repo := path.Split(t.size)
	return []string{t.size, hub + ".locks/" + repo}
}

// start launches the download. Only the artifact's own cache subtree is mounted, read-write; the
// token travels as an environment variable of this one container and is never written to disk by
// the host.
func (c *Cache) start(ctx context.Context, t target, img, token string) error {
	id := t.art.ArtifactID
	if !t.hf {
		// A plain download writes straight to its final name, so what an interrupted one left
		// behind cannot be trusted. Only that is deleted: the breadcrumb says the contents are
		// ours, and no replica may have the path mounted. Anything else was adopted by Sync.
		if _, err := os.Stat(c.fetchingPath(id)); err == nil && cached(c.root(), t.model) {
			paths, ok, err := c.replicaPaths(ctx, false)
			if err != nil {
				return err
			}
			if !ok || paths[t.model.path] {
				return fmt.Errorf("%s holds a partial download but a replica may mount it; not deleting it", t.model.path)
			}
			if err := os.RemoveAll(c.host(t.model.path)); err != nil {
				return err
			}
		}
		if err := os.MkdirAll(filepath.Dir(c.fetchingPath(id)), 0o700); err != nil {
			return err
		}
		if err := writeDurable(c.fetchingPath(id), []byte(t.model.path)); err != nil {
			return err
		}
	}
	for _, d := range prefetchDirs(t) {
		if err := os.MkdirAll(c.host(d), 0o755); err != nil {
			return err
		}
	}
	hfHome := containerModelRoot + "/hf"
	if t.art.OwnerProject != "" {
		hfHome = containerModelRoot + "/p/" + t.art.OwnerProject + "/hf"
	}
	// The xet chunk cache would otherwise land in HF_HOME, which is not mounted.
	env := []string{"HF_HOME=" + hfHome, "HF_XET_CACHE=/tmp/xet", "HOME=/tmp", "HF_HUB_DISABLE_PROGRESS_BARS=1"}
	if token != "" {
		env = append(env, "HF_TOKEN="+token)
	}
	spec := ContainerCreate{
		Image:      img,
		Entrypoint: []string{"python3", "-c"},
		Cmd:        []string{t.script},
		Env:        env,
		Labels: map[string]string{
			LabelManagedBy: prefetchManagedBy, LabelArtifact: id, LabelModelPath: t.model.path,
			LabelProject: t.art.OwnerProject,
		},
		HostConfig: HostConfig{
			Binds:          c.prefetchBinds(t),
			SecurityOpt:    []string{"no-new-privileges"},
			CapDrop:        []string{"ALL"},
			ReadonlyRootfs: true,
			Tmpfs:          map[string]string{"/tmp": "rw,nosuid,nodev"},
			RestartPolicy:  RestartPolicy{Name: "no"},
		},
	}
	name := prefetchName(id)
	// A container left by an earlier run under this name is stale: collect only keeps running
	// ones, and those are never restarted.
	_ = c.Docker.Remove(ctx, name)
	cid, err := c.Docker.Create(ctx, name, spec)
	if errors.Is(err, ErrNoSuchImage) {
		if perr := c.Docker.Pull(ctx, img); perr != nil {
			return &pullError{image: img, err: perr}
		}
		cid, err = c.Docker.Create(ctx, name, spec)
	}
	if err != nil {
		return err
	}
	if err := c.Docker.Start(ctx, cid); err != nil {
		_ = c.Docker.Remove(ctx, cid)
		return err
	}
	c.log().Info("prefetch started", "artifact", id, "image", img)
	return nil
}

// evict removes resident artifacts outside the wanted set and not mounted by any replica
// container, least recently used first, from above the high watermark down to the low one.
func (c *Cache) evict(ctx context.Context, wanted map[string]bool) []string {
	high, low := c.HighWatermark, c.LowWatermark
	if high <= 0 {
		high = DefaultHighWatermark
	}
	if low <= 0 || low > high {
		low = min(DefaultLowWatermark, high)
	}
	used, total, err := c.usage()
	if err != nil || total == 0 || float64(used)/float64(total) <= high {
		return nil
	}
	// Only a running or restarting replica holds its weights; a stopped one's are fair game and
	// it fetches them again when it starts.
	mounted, ok, err := c.replicaPaths(ctx, true)
	if err != nil {
		return nil
	}
	if !ok {
		// A live replica from before the label existed: what it mounts is unknown, so nothing
		// is safe to remove until it is replaced.
		c.log().Warn("cache over its high watermark but a running replica has no model-path label; not evicting")
		return nil
	}
	markers := c.markers()
	// Touch what is mounted, so LRU order follows use, not completion.
	now := c.now()
	for i, m := range markers {
		if mounted[m.Path] && now.Sub(m.LastUsedAt) > 10*time.Minute {
			markers[i].LastUsedAt = now
			_ = c.writeMarker(markers[i])
		}
	}
	sort.Slice(markers, func(i, j int) bool { return markers[i].LastUsedAt.Before(markers[j].LastUsedAt) })
	var evicted []string
	for _, m := range markers {
		if float64(used)/float64(total) <= low {
			break
		}
		if wanted[m.ArtifactID] || mounted[m.Path] {
			continue
		}
		if err := c.remove(m, markers, mounted); err != nil {
			c.log().Warn("eviction failed", "artifact", m.ArtifactID, "error", err)
			continue
		}
		c.log().Info("evicted", "artifact", m.ArtifactID, "bytes", m.Bytes)
		evicted = append(evicted, m.ArtifactID)
		if used, total, err = c.usage(); err != nil || total == 0 {
			break
		}
	}
	return evicted
}

// remove deletes an artifact's marker, then its weights. A Hugging Face snapshot shares its repo's
// blob store with other revisions, so the repo goes only when no other marker or replica uses it;
// otherwise only the snapshot does.
func (c *Cache) remove(m marker, all []marker, mounted map[string]bool) error {
	if err := os.Remove(c.markerPath(m.ArtifactID)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	target := m.Path
	if i := strings.Index(m.Path, "/snapshots/"); i >= 0 {
		repo := m.Path[:i]
		shared := false
		for _, o := range all {
			if o.ArtifactID != m.ArtifactID && strings.HasPrefix(o.Path, repo+"/") {
				shared = true
			}
		}
		for p := range mounted {
			if strings.HasPrefix(p, repo+"/") {
				shared = true
			}
		}
		if !shared {
			target = repo
		}
	}
	return os.RemoveAll(c.host(target))
}

func (c *Cache) markers() []marker {
	entries, err := os.ReadDir(filepath.Join(c.root(), filepath.FromSlash(markerDir)))
	if err != nil {
		return nil
	}
	var out []marker
	for _, e := range entries {
		id, ok := strings.CutSuffix(e.Name(), ".json")
		if !ok || !artifactIDPattern.MatchString(id) {
			continue
		}
		if m, ok := c.readMarker(id); ok {
			out = append(out, m)
		}
	}
	return out
}

// dirSize is the bytes under dir, symlinks counted as themselves (a snapshot links into blobs).
func dirSize(dir string) int64 {
	var total int64
	_ = filepath.WalkDir(dir, func(_ string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil //nolint:nilerr // a vanished file is not worth failing a size over
		}
		if info, err := d.Info(); err == nil && !d.IsDir() {
			total += info.Size()
		}
		return nil
	})
	return total
}
