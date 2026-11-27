package serve

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/backend"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/observe"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// Supply is the inventory side, which the host's observer already reads.
type Supply interface {
	Inventory(ctx context.Context) (backend.Inventory, error)
}

// Backend runs container deployments on the local Docker Engine.
type Backend struct {
	Docker      Docker
	Allocations *Allocations
	Policy      Policy
	ClusterID   string
	// ModelRoot is the host's model cache; DefaultModelRoot when empty.
	ModelRoot string
	// GPUs lists the node name and the UUIDs of its healthy accelerators.
	GPUs   func(ctx context.Context) (node string, uuids []string, err error)
	Supply Supply
	// CDI requests GPUs as CDI devices (nvidia.com/gpu=<uuid>) instead of through the NVIDIA
	// runtime hook.
	CDI bool
	// Probe is the client readiness probes run with. Nil uses a 2s timeout.
	Probe *http.Client
	Log   *slog.Logger
	Now   func() time.Time

	mu    sync.Mutex
	ready map[string][]int
}

var _ backend.Backend = (*Backend)(nil)

func (b *Backend) log() *slog.Logger {
	if b.Log == nil {
		return slog.Default()
	}
	return b.Log
}

func (b *Backend) now() time.Time {
	if b.Now == nil {
		return time.Now().UTC()
	}
	return b.Now().UTC()
}

func (b *Backend) modelRoot() string {
	if b.ModelRoot == "" {
		return DefaultModelRoot
	}
	return b.ModelRoot
}

// WorkloadName is the slug replica containers are named after: ws-<name>-<replica>.
func WorkloadName(dep wire.DeploymentDesired) string {
	if s := slug(dep.Name); s != "" {
		return s
	}
	return slug(dep.DeploymentID)
}

func slug(in string) string {
	var sb strings.Builder
	dash := false
	for _, r := range strings.ToLower(in) {
		switch {
		case (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9'):
			sb.WriteRune(r)
			dash = false
		case sb.Len() > 0 && !dash:
			sb.WriteByte('-')
			dash = true
		}
	}
	out := strings.Trim(sb.String(), "-")
	if len(out) > 48 {
		out = strings.Trim(out[:48], "-")
	}
	return out
}

func containerName(workload string, replica int) string {
	return "ws-" + workload + "-" + strconv.Itoa(replica)
}

func replicas(dep wire.DeploymentDesired) int {
	if dep.MinReplicas < 0 {
		return 0
	}
	return dep.MinReplicas
}

func devicesPerReplica(l backend.Layout, dep wire.DeploymentDesired) int {
	if l.DevicesPerNode > 0 {
		return l.DevicesPerNode
	}
	if dep.Placement.DevicesPerReplica > 0 {
		return dep.Placement.DevicesPerReplica
	}
	return 1
}

func layoutFingerprint(l backend.Layout) string {
	return fmt.Sprintf("%dx%d/%s", max(l.NodesPerReplica, 1), l.DevicesPerNode, l.Domain)
}

// plan is a validated deployment, ready to be turned into create bodies.
type plan struct {
	dep      wire.DeploymentDesired
	model    model
	port     int
	probe    probe
	workload string
	devices  int
	layout   string
}

func (b *Backend) plan(d backend.Desired) (plan, error) {
	dep := d.Deployment
	if !dep.Serving.IsContainer() {
		return plan{}, reject("deployment %s: a host runs runtime: container only, not %q", dep.DeploymentID, dep.Serving.Runtime)
	}
	if dep.Serving.Topology == "disaggregated" || d.Layout.NodesPerReplica > 1 {
		return plan{}, reject("deployment %s: a host runs one aggregated container per replica", dep.DeploymentID)
	}
	c := dep.Serving.Container
	if err := b.Policy.checkImage(dep.DeploymentID, c.Image); err != nil {
		return plan{}, err
	}
	port := 0
	for _, p := range c.Ports {
		if p.Name == httpPort {
			port = p.ContainerPort
		}
	}
	if port <= 0 || port > 65535 {
		return plan{}, reject("deployment %s: the container declares no port named %s", dep.DeploymentID, httpPort)
	}
	m, err := modelFor(d.Artifact)
	if err != nil {
		return plan{}, err
	}
	pr, err := readinessProbe(c, port)
	if err != nil {
		return plan{}, reject("deployment %s: readiness probe: %v", dep.DeploymentID, err)
	}
	workload := WorkloadName(dep)
	if workload == "" {
		return plan{}, reject("deployment has no id")
	}
	return plan{
		dep: dep, model: m, port: port, probe: pr, workload: workload,
		devices: devicesPerReplica(d.Layout, dep), layout: layoutFingerprint(d.Layout),
	}, nil
}

// create is the body for one replica holding gpus.
func (b *Backend) create(p plan, replica int, gpus []string) ContainerCreate {
	dep, c := p.dep, p.dep.Serving.Container
	modelPath := containerModelRoot + "/" + p.model.path
	injected := map[string]string{
		EnvModelPath:       modelPath,
		EnvServedModelName: strings.TrimSpace(dep.ServedModelName),
		EnvPort:            strconv.Itoa(p.port),
	}
	env := []string{
		EnvModelPath + "=" + injected[EnvModelPath],
		EnvServedModelName + "=" + injected[EnvServedModelName],
		EnvPort + "=" + injected[EnvPort],
		"HF_HUB_OFFLINE=1",
	}
	vars := map[string]string{}
	for k, v := range injected {
		vars[k] = v
	}
	for _, e := range c.Env {
		env = append(env, e.Name+"="+e.Value)
		vars[e.Name] = e.Value
	}
	exposed := map[string]struct{}{}
	for _, port := range c.Ports {
		exposed[tcp(port.ContainerPort)] = struct{}{}
	}
	hc := HostConfig{
		Binds: []string{
			filepath.Join(b.modelRoot(), filepath.FromSlash(p.model.dir)) + ":" + containerModelRoot + "/" + p.model.dir + ":ro",
		},
		// Loopback only and an ephemeral port: the agent's tunnel is the only way in.
		PortBindings:   map[string][]PortBinding{tcp(p.port): {{HostIP: "127.0.0.1", HostPort: ""}}},
		SecurityOpt:    []string{"no-new-privileges"},
		CapDrop:        []string{"ALL"},
		ReadonlyRootfs: true,
		Tmpfs:          map[string]string{"/tmp": "rw,nosuid,nodev"},
		// Engines share tensors through /dev/shm; Docker's 64MiB default fails multi-GPU loads.
		ShmSize:       1 << 30,
		RestartPolicy: RestartPolicy{Name: "unless-stopped"},
	}
	if len(gpus) > 0 {
		if b.CDI {
			ids := make([]string, len(gpus))
			for i, u := range gpus {
				ids[i] = "nvidia.com/gpu=" + u
			}
			hc.DeviceRequests = []DeviceRequest{{Driver: "cdi", DeviceIDs: ids}}
		} else {
			hc.DeviceRequests = []DeviceRequest{{Driver: "nvidia", DeviceIDs: gpus, Capabilities: [][]string{{"gpu"}}}}
		}
	}
	if n := dep.Serving.Resources.MemoryMiB; n != nil {
		hc.Memory = *n * 1024 * 1024
	}
	if n := dep.Serving.Resources.CPUCores; n != nil {
		hc.NanoCPUs = int64(*n) * 1e9
	}
	return ContainerCreate{
		Image:      c.Image,
		Entrypoint: expand(c.Command, vars),
		Cmd:        expand(c.Args, vars),
		Env:        env,
		Labels: map[string]string{
			LabelManagedBy:  managedBy,
			LabelDeployment: dep.DeploymentID,
			LabelProject:    dep.ProjectID,
			LabelGeneration: strconv.FormatInt(dep.Generation, 10),
			LabelDigest:     dep.Serving.Digest,
			LabelLayout:     p.layout,
			LabelReplica:    strconv.Itoa(replica),
			LabelGPUs:       strings.Join(gpus, ","),
			LabelPort:       strconv.Itoa(p.port),
		},
		ExposedPorts: exposed,
		HostConfig:   hc,
	}
}

// current says a container was built from this generation, digest and layout.
func current(c ContainerSummary, p plan) bool {
	l := c.Labels
	return l[LabelGeneration] == strconv.FormatInt(p.dep.Generation, 10) &&
		l[LabelDigest] == p.dep.Serving.Digest && l[LabelLayout] == p.layout
}

func (b *Backend) list(ctx context.Context, deploymentID string) ([]ContainerSummary, error) {
	labels := map[string]string{LabelManagedBy: managedBy}
	if deploymentID != "" {
		labels[LabelDeployment] = deploymentID
	}
	return b.Docker.List(ctx, labels)
}

// Adopt rebuilds the allocation map from the labels of the containers that exist, after a
// restart or a lost state file.
func (b *Backend) Adopt(ctx context.Context) error {
	all, err := b.list(ctx, "")
	if err != nil {
		return err
	}
	live := map[string]Grant{}
	for _, c := range all {
		var gpus []string
		if s := c.Labels[LabelGPUs]; s != "" {
			gpus = strings.Split(s, ",")
		}
		live[c.Name()] = Grant{Deployment: c.Labels[LabelDeployment], GPUs: gpus}
	}
	b.log().Info("adopted replica containers", "containers", len(live))
	return b.Allocations.Adopt(live)
}

// Apply implements backend.Backend. Each replica slot is kept if its container was built from
// these inputs, and otherwise replaced: the old one is removed first, because the accelerators
// it holds are the ones the new one needs.
func (b *Backend) Apply(ctx context.Context, d backend.Desired) (backend.Applied, error) {
	p, err := b.plan(d)
	if err != nil {
		return backend.Applied{}, err
	}
	if !cached(b.modelRoot(), p.model) {
		return backend.Applied{}, fmt.Errorf("%w: %s is missing or empty (prefetch on hosts is not available yet; populate it by hand)",
			ErrWeightsNotCached, filepath.Join(b.modelRoot(), filepath.FromSlash(p.model.path)))
	}
	if d.Hold != nil {
		if err := d.Hold(ctx); err != nil {
			return backend.Applied{}, err
		}
	}
	existing, err := b.list(ctx, p.dep.DeploymentID)
	if err != nil {
		return backend.Applied{}, err
	}
	byName := map[string]ContainerSummary{}
	for _, c := range existing {
		byName[c.Name()] = c
	}
	n := replicas(p.dep)
	changed := false
	for i := range n {
		name := containerName(p.workload, i)
		if c, ok := byName[name]; ok {
			if current(c, p) {
				continue
			}
			if err := b.remove(ctx, c); err != nil {
				return backend.Applied{}, err
			}
		}
		if err := b.start(ctx, p, i, name); err != nil {
			return backend.Applied{}, err
		}
		changed = true
	}
	if changed {
		b.log().Info("applied", "deployment", p.dep.DeploymentID, "generation", p.dep.Generation,
			"replicas", n, "runtime", wire.RuntimeContainer)
	}
	return backend.Applied{Name: p.workload, Unchanged: !changed}, nil
}

func (b *Backend) start(ctx context.Context, p plan, replica int, name string) error {
	var gpus []string
	if p.devices > 0 {
		_, healthy, err := b.GPUs(ctx)
		if err != nil {
			return fmt.Errorf("read accelerators: %w", err)
		}
		gpus, err = b.Allocations.Allocate(name, p.dep.DeploymentID, p.devices, healthy)
		if err != nil {
			return err
		}
	}
	spec := b.create(p, replica, gpus)
	id, err := b.Docker.Create(ctx, name, spec)
	if errors.Is(err, ErrNoSuchImage) {
		if perr := b.Docker.Pull(ctx, spec.Image); perr != nil {
			_ = b.Allocations.Release(name)
			return &pullError{image: spec.Image, err: perr}
		}
		id, err = b.Docker.Create(ctx, name, spec)
	}
	if err != nil {
		_ = b.Allocations.Release(name)
		return fmt.Errorf("create %s: %w", name, err)
	}
	if err := b.Docker.Start(ctx, id); err != nil {
		return fmt.Errorf("start %s: %w", name, err)
	}
	return nil
}

// pullError is an image the daemon could not pull, reported as ImagePullFailed.
type pullError struct {
	image string
	err   error
}

func (e *pullError) Error() string { return fmt.Sprintf("pull %s: %v", e.image, e.err) }
func (e *pullError) Unwrap() error { return e.err }

func (b *Backend) remove(ctx context.Context, c ContainerSummary) error {
	if err := b.Docker.Remove(ctx, c.ID); err != nil {
		return fmt.Errorf("remove %s: %w", c.Name(), err)
	}
	b.log().Info("removed replica container", "deployment", c.Labels[LabelDeployment], "name", c.Name())
	return b.Allocations.Release(c.Name())
}

// Prune implements backend.Backend: every container of the deployment that is not one of the
// current workload's replica slots goes, which covers a rename and a scale-down.
func (b *Backend) Prune(ctx context.Context, dep wire.DeploymentDesired, workload string) error {
	existing, err := b.list(ctx, dep.DeploymentID)
	if err != nil {
		return err
	}
	keep := map[string]bool{}
	for i := range replicas(dep) {
		keep[containerName(workload, i)] = true
	}
	for _, c := range existing {
		if !keep[c.Name()] {
			if err := b.remove(ctx, c); err != nil {
				return err
			}
		}
	}
	return nil
}

// Delete implements backend.Backend.
func (b *Backend) Delete(ctx context.Context, deploymentID string) error {
	existing, err := b.list(ctx, deploymentID)
	if err != nil {
		return err
	}
	for _, c := range existing {
		if err := b.remove(ctx, c); err != nil {
			return err
		}
	}
	b.setReady(deploymentID, nil)
	return nil
}

// Managed lists the deployment ids that have containers here.
func (b *Backend) Managed(ctx context.Context) (map[string]bool, error) {
	all, err := b.list(ctx, "")
	if err != nil {
		return nil, err
	}
	out := map[string]bool{}
	for _, c := range all {
		out[c.Labels[LabelDeployment]] = true
	}
	return out, nil
}

// Inventory implements backend.Backend.
func (b *Backend) Inventory(ctx context.Context) (backend.Inventory, error) {
	if b.Supply == nil {
		return backend.Inventory{}, errors.New("host backend: no inventory source")
	}
	return b.Supply.Inventory(ctx)
}

// crashLoopRestarts is how many restarts read as a crash loop rather than a slow start.
const crashLoopRestarts = 3

// Observe implements backend.Backend: readiness is the declared probe, run from here against the
// replica's loopback port.
func (b *Backend) Observe(ctx context.Context, dep wire.DeploymentDesired) (backend.Rollout, error) {
	at := b.now()
	stamp := at.Format(time.RFC3339Nano)
	existing, err := b.list(ctx, dep.DeploymentID)
	if err != nil {
		return backend.Rollout{}, err
	}
	sort.Slice(existing, func(i, j int) bool { return existing[i].Name() < existing[j].Name() })
	node := ""
	if b.GPUs != nil {
		node, _, _ = b.GPUs(ctx)
	}
	nodeID := observe.NodeID(b.ClusterID, node)
	pr := probe{Path: "/health", Scheme: "http"}
	if c := dep.Serving.Container; c != nil {
		port := 0
		for _, p := range c.Ports {
			if p.Name == httpPort {
				port = p.ContainerPort
			}
		}
		if got, err := readinessProbe(c, port); err == nil {
			pr = got
		}
	}

	out := backend.Rollout{Desired: replicas(dep), ObservedAt: at, Replicas: []wire.ReplicaObserved{}}
	var ready []int
	var trouble *wire.LastError
	reason := ""
	for _, c := range existing {
		info, err := b.Docker.Inspect(ctx, c.ID)
		if errors.Is(err, ErrNotFound) {
			continue
		}
		if err != nil {
			return backend.Rollout{}, err
		}
		gen, _ := strconv.ParseInt(c.Labels[LabelGeneration], 10, 64)
		updated := gen == dep.Generation && c.Labels[LabelDigest] == dep.Serving.Digest
		var gpus []string
		if s := c.Labels[LabelGPUs]; s != "" {
			gpus = strings.Split(s, ",")
		}
		deviceIDs := make([]string, len(gpus))
		for i, u := range gpus {
			deviceIDs[i] = observe.DeviceID(b.ClusterID, u)
		}
		hostPort := publishedPort(info, pr.Port)
		ok := info.State.Running && hostPort > 0 && b.probe(ctx, pr, hostPort)
		phase := "pending"
		switch {
		case ok:
			phase = "running"
			if updated {
				ready = append(ready, publishedPort(info, atoi(c.Labels[LabelPort])))
			}
		case info.State.Running:
			phase = "starting"
		case info.RestartCount >= crashLoopRestarts || info.State.Restarting:
			phase = "failed"
			reason = "CrashLooping"
			trouble = &wire.LastError{Code: "CrashLooping", Message: fmt.Sprintf(
				"%s exited with %d and restarted %d times", info.Name, info.State.ExitCode, info.RestartCount), At: stamp}
		case info.State.Status == "exited" || info.State.Status == "dead":
			phase = "failed"
		}
		if ok && updated {
			out.Ready++
		}
		if updated {
			out.Updated++
		}
		replicaID := observe.ReplicaID(b.ClusterID, "host", c.Name())
		var started *string
		if s := info.State.StartedAt; s != "" && !strings.HasPrefix(s, "0001-") {
			started = &s
		}
		out.Replicas = append(out.Replicas, wire.ReplicaObserved{
			ReplicaID: replicaID, Name: c.Name(), DeploymentID: dep.DeploymentID, ClusterID: b.ClusterID,
			NodeID: nodeID, DeviceIDs: deviceIDs, Phase: phase, Ready: ok,
			Engine: wire.RuntimeContainer, ServingDigest: c.Labels[LabelDigest],
			RestartCount: info.RestartCount, ObservedGeneration: gen, StartedAt: started, ObservedAt: stamp,
		})
		if len(deviceIDs) > 0 {
			out.Allocations = append(out.Allocations, wire.AllocationObserved{
				AllocationID: observe.AllocationID(b.ClusterID, "host", c.Name()), ClusterID: b.ClusterID,
				ReplicaID: replicaID, DeploymentID: dep.DeploymentID, ProjectID: dep.ProjectID,
				NodeID: nodeID, DeviceIDs: deviceIDs, Exclusive: true, GrantedAt: stamp, ObservedAt: stamp,
			})
		}
	}
	out.Unavailable = max(out.Desired-out.Ready, 0)
	out.LastError = trouble
	switch {
	case out.Desired == 0:
		out.Phase, out.Reason = "placed", "Applied"
	case reason != "":
		out.Phase, out.Reason = "failed", reason
	case out.Ready == 0:
		out.Phase, out.Reason = "placed", "Applied"
	case out.Ready < out.Desired:
		out.Phase, out.Reason = "degraded", "Progressing"
	default:
		out.Phase, out.Reason = "running", "Serving"
	}
	out.Message = fmt.Sprintf("%d of %d replicas ready", out.Ready, out.Desired)
	if trouble != nil {
		out.Message += ": " + trouble.Message
	}
	out.Converging = out.Ready < out.Desired
	b.setReady(dep.DeploymentID, ready)
	return out, nil
}

func atoi(s string) int {
	n, _ := strconv.Atoi(s)
	return n
}

// publishedPort is the loopback port a container port is published on, or 0.
func publishedPort(info ContainerInspect, containerPort int) int {
	for _, b := range info.NetworkSettings.Ports[tcp(containerPort)] {
		if b.HostIP == "127.0.0.1" || b.HostIP == "" {
			if n := atoi(b.HostPort); n > 0 {
				return n
			}
		}
	}
	return 0
}

func (b *Backend) probe(ctx context.Context, p probe, hostPort int) bool {
	client := b.Probe
	if client == nil {
		client = &http.Client{Timeout: 2 * time.Second, Transport: &http.Transport{
			// The replica is on loopback and serves a self-signed certificate if any.
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec
		}}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		fmt.Sprintf("%s://127.0.0.1:%d%s", p.Scheme, hostPort, p.Path), nil)
	if err != nil {
		return false
	}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	_ = resp.Body.Close()
	return resp.StatusCode >= 200 && resp.StatusCode < 400
}

func (b *Backend) setReady(id string, ports []int) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.ready == nil {
		b.ready = map[string][]int{}
	}
	if len(ports) == 0 {
		delete(b.ready, id)
		return
	}
	b.ready[id] = ports
}

// Ready is the loopback ports of each deployment's ready, current replicas, for the resolver.
func (b *Backend) Ready() map[string][]int {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make(map[string][]int, len(b.ready))
	for id, p := range b.ready {
		out[id] = append([]int(nil), p...)
	}
	return out
}
