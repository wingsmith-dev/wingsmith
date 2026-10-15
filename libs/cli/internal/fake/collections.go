package fake

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
)

// The filters here are the ones the plane's routes declare, and only those. A
// filter the server does not implement is a filter the fake must not honour,
// or the fake becomes a place where wrong client behaviour looks right.

// fakeAiperfImage is the harness image the seeded runs were taken with.
const fakeAiperfImage = "nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0"

func (b *Backend) Clusters(ctx context.Context, o api.ListOpts) (api.Page[api.Cluster], error) {
	return collect(b, b.state.Clusters, o, func(c api.Cluster) bool {
		return eqFold(o.Region, c.Region) && eqFold(o.Phase, c.Phase) &&
			contains(o.Search, c.Name, c.ID, c.Region, deref(c.DisplayName)) &&
			labelsMatch(c.Labels, o.Labels)
	})
}

func (b *Backend) Cluster(ctx context.Context, ref string) (api.Cluster, error) {
	return find(b, "cluster", b.state.Clusters, ref, func(c api.Cluster) []string {
		return []string{c.Name, c.ID}
	})
}

func (b *Backend) Nodes(ctx context.Context, o api.ListOpts) (api.Page[api.Node], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Nodes, o, func(n api.Node) bool {
		return refEq(cluster, n.Cluster, n.ClusterName) &&
			refEqPtr(o.Pool, n.Pool, n.PoolName) &&
			eqFold(o.Condition, n.Condition) &&
			boolEq(o.Ready, n.Ready) && boolEq(o.Cordoned, n.Cordoned) &&
			contains(o.Search, n.Name, n.ID, n.ClusterName, deref(n.PoolName)) &&
			labelsMatch(n.Labels, o.Labels)
	})
}

func (b *Backend) Node(ctx context.Context, ref string) (api.Node, error) {
	return find(b, "node", b.state.Nodes, ref, func(n api.Node) []string {
		return []string{n.Name, n.ID}
	})
}

func (b *Backend) Devices(ctx context.Context, o api.ListOpts) (api.Page[api.Device], error) {
	cluster := b.clusterRef(o.Cluster)
	node := b.nodeRef(o.Node)
	return collect(b, b.state.Devices, o, func(d api.Device) bool {
		return refEq(cluster, d.Cluster, d.ClusterName) &&
			refEq(node, d.Node, d.NodeName) &&
			b.devicePool(d, o.Pool) &&
			eqFold(o.Class, d.DeviceClass) && eqFold(o.Health, d.Health) &&
			boolEq(o.Allocatable, d.Allocatable) &&
			deviceInDomain(d, o.TopologyDomain)
	})
}

func (b *Backend) Device(ctx context.Context, ref string) (api.Device, error) {
	return find(b, "device", b.state.Devices, ref, func(d api.Device) []string {
		return []string{d.ID, d.DeviceUUID}
	})
}

// DeviceClasses derives the catalog from the seeded devices rather than carrying a table of its
// own. The contract dictionary is embedded in the plane and in the operator, and a third copy here
// would be a third thing to keep in step — so every class the fake reports is one it can see
// hardware for, and none of them claim a catalog entry.
func (b *Backend) DeviceClasses(ctx context.Context, o api.ListOpts) (api.Page[api.DeviceClass], error) {
	classes := b.deviceClasses(o.Cluster)
	return collect(b, classes, o, func(c api.DeviceClass) bool {
		return eqFold(o.Architecture, c.Architecture) && boolEq(o.Present, c.Inventory.Devices > 0)
	})
}

func (b *Backend) DeviceClass(ctx context.Context, ref string) (api.DeviceClass, error) {
	return find(b, "device class", b.deviceClasses(""), ref, func(c api.DeviceClass) []string {
		return []string{c.Class}
	})
}

func (b *Backend) deviceClasses(cluster string) []api.DeviceClass {
	ref := b.clusterRef(cluster)
	byClass := map[string]*api.DeviceClass{}
	nodes := map[string]map[string]bool{}
	clusters := map[string]map[string]bool{}
	order := []string{}

	for _, d := range b.state.Devices {
		if !refEq(ref, d.Cluster, d.ClusterName) {
			continue
		}
		entry, seen := byClass[d.DeviceClass]
		if !seen {
			entry = &api.DeviceClass{
				Class:     d.DeviceClass,
				Label:     d.DeviceClass,
				Vendor:    d.Vendor,
				MemoryMiB: d.MemoryMiB,
				Aliases:   map[string][]string{},
				Inventory: api.DeviceClassInventory{Clusters: []string{}},
			}
			byClass[d.DeviceClass] = entry
			nodes[d.DeviceClass] = map[string]bool{}
			clusters[d.DeviceClass] = map[string]bool{}
			order = append(order, d.DeviceClass)
		}
		entry.Inventory.Devices++
		if d.Health == api.HealthHealthy {
			entry.Inventory.Healthy++
		}
		if d.Allocatable {
			entry.Inventory.Allocatable++
		} else {
			entry.Inventory.Allocated++
		}
		nodes[d.DeviceClass][d.Node] = true
		clusters[d.DeviceClass][d.ClusterName] = true
	}

	out := make([]api.DeviceClass, 0, len(order))
	for _, name := range order {
		entry := byClass[name]
		entry.Inventory.Nodes = len(nodes[name])
		for c := range clusters[name] {
			entry.Inventory.Clusters = append(entry.Inventory.Clusters, c)
		}
		sort.Strings(entry.Inventory.Clusters)
		out = append(out, *entry)
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Inventory.Devices != out[j].Inventory.Devices {
			return out[i].Inventory.Devices > out[j].Inventory.Devices
		}
		return out[i].Class < out[j].Class
	})
	return out
}

func (b *Backend) TopologyDomains(ctx context.Context, o api.ListOpts) (api.Page[api.TopologyDomain], error) {
	cluster := b.clusterRef(o.Cluster)
	node := b.nodeRef(o.Node)
	return collect(b, b.state.TopologyDomains, o, func(t api.TopologyDomain) bool {
		return refEq(cluster, t.Cluster, t.ClusterName) &&
			refEqAny(node, t.Nodes, t.NodeNames) &&
			eqFold(o.Kind, t.Kind) && boolEq(o.SpansNodes, t.SpansNodes)
	})
}

func (b *Backend) TopologyDomain(ctx context.Context, ref string) (api.TopologyDomain, error) {
	return find(b, "topology-domain", b.state.TopologyDomains, ref, func(t api.TopologyDomain) []string {
		return []string{t.Identifier, t.ID}
	})
}

func (b *Backend) Pools(ctx context.Context, o api.ListOpts) (api.Page[api.Pool], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Pools, o, func(p api.Pool) bool {
		return refEq(cluster, p.ClusterID) && eqFold(o.Class, p.DeviceClass) &&
			boolEq(o.Enabled, p.Enabled) &&
			contains(o.Search, p.Name, p.ID, p.DeviceClass) &&
			labelsMatch(p.Labels, o.Labels)
	})
}

func (b *Backend) Pool(ctx context.Context, ref string) (api.Pool, error) {
	return find(b, "pool", b.state.Pools, ref, func(p api.Pool) []string {
		return []string{p.Name, p.ID}
	})
}

func (b *Backend) Artifacts(ctx context.Context, o api.ListOpts) (api.Page[api.Artifact], error) {
	return collect(b, b.state.Artifacts, o, func(a api.Artifact) bool {
		return eqFold(o.Kind, a.Kind) && eqFold(o.Format, deref(a.Format)) &&
			refEqPtr(o.Project, a.ProjectID, nil) &&
			contains(o.Search, a.Name, a.ID, a.Digest)
	})
}

func (b *Backend) Artifact(ctx context.Context, ref string) (api.Artifact, error) {
	return find(b, "artifact", b.state.Artifacts, ref, func(a api.Artifact) []string {
		return []string{a.Name, a.ID, a.Digest}
	})
}

// Workloads answers with the deployments and the rollup filled in from the
// per-cluster rows, rather than from a copy stored beside them. A workload's
// scale is a sum of its shares, and a fake that kept its own answer to that
// would be the one place in the system where the two could disagree.
func (b *Backend) Workloads(ctx context.Context, o api.ListOpts) (api.Page[api.Workload], error) {
	cluster := b.clusterRef(o.Cluster)
	hydrated := make([]api.Workload, 0, len(b.state.Workloads))
	for _, wl := range b.state.Workloads {
		hydrated = append(hydrated, b.hydrateWorkload(wl))
	}
	return collect(b, hydrated, o, func(wl api.Workload) bool {
		return refEq(o.Project, wl.ProjectID) && eqFold(o.Kind, wl.Kind) &&
			eqFold(o.Phase, wl.Phase) && refEq(o.Artifact, wl.Serving.ArtifactID) &&
			eqFold(o.Class, wl.Serving.Shape.DeviceClass) &&
			eqFold(o.ServingDigest, wl.ServingDigest) &&
			workloadOnCluster(wl, cluster) &&
			contains(o.Search, wl.Name, wl.ID) &&
			labelsMatch(wl.Labels, o.Labels)
	})
}

func (b *Backend) Workload(ctx context.Context, ref string) (api.Workload, error) {
	wl, err := find(b, "workload", b.state.Workloads, ref, func(wl api.Workload) []string {
		return []string{wl.Name, wl.ID}
	})
	if err != nil {
		return wl, err
	}
	return b.hydrateWorkload(wl), nil
}

func workloadOnCluster(wl api.Workload, cluster string) bool {
	if cluster == "" {
		return true
	}
	for _, d := range wl.Deployments {
		if refEq(cluster, d.ClusterID, d.ClusterName) {
			return true
		}
	}
	return false
}

// hydrateWorkload derives everything a workload only knows through its
// deployments: the per-cluster table, the placed and ready sums, and the phase.
func (b *Backend) hydrateWorkload(wl api.Workload) api.Workload {
	wl.Deployments = []api.WorkloadDeployment{}
	placed, ready := 0, 0
	for _, d := range b.state.Deployments {
		if d.WorkloadID != wl.ID {
			continue
		}
		clusterName, region := b.clusterIdentity(d.ClusterID)
		wl.Deployments = append(wl.Deployments, api.WorkloadDeployment{
			ID: d.ID, Name: d.Name, ClusterID: d.ClusterID, ClusterName: clusterName, Region: region,
			MinReplicas: d.MinReplicas, MaxReplicas: d.MaxReplicas, DesiredReplicas: d.DesiredReplicas,
			AdmissionPhase: d.AdmissionPhase, AdmissionReason: d.AdmissionReason,
			Generation: d.Generation, Enabled: d.Enabled, Authority: d.Authority,
			QueuedAt: d.QueuedAt, AdmittedAt: d.AdmittedAt, PlacedAt: d.PlacedAt,
			Replicas: d.Replicas, Status: d.Status,
			LiveDigests: map[string]int{},
		})
		if d.Replicas.Ready > 0 {
			wl.Deployments[len(wl.Deployments)-1].LiveDigests[wl.ServingDigest] = d.Replicas.Ready
		}
		placed += d.DesiredReplicas
		ready += d.Replicas.Ready
	}
	wl.Scale.Unit = scaleUnit(wl.Kind)
	wl.Scale.Placed = placed
	wl.Scale.Ready = ready
	wl.Phase = workloadPhase(wl.Deployments)
	// The model id the engine registers under is the artifact's slug, derived
	// on read and never declared.
	for _, a := range b.state.Artifacts {
		if a.ID == wl.Serving.ArtifactID {
			wl.ServedModelName = orElse(a.Slug, a.Name)
		}
	}
	return wl
}

// scaleUnit names what the three numbers count. The plane derives it from kind
// for the same reason: a bare integer in a listing is unreadable without it.
func scaleUnit(kind string) string {
	switch kind {
	case "gang":
		return "ranks"
	case "task":
		return "completions"
	}
	return "replicas"
}

// workloadPhase is the least advanced phase among the deployments, with failure
// winning outright. Nothing writes it — a workload running in one region and
// queued on quota in another is not running.
func workloadPhase(deployments []api.WorkloadDeployment) string {
	if len(deployments) == 0 {
		return "pending"
	}
	order := map[string]int{"pending": 0, "admitted": 1, "placed": 2, "running": 3}
	least := "running"
	for _, d := range deployments {
		if d.AdmissionPhase == "failed" {
			return "failed"
		}
		if order[d.AdmissionPhase] < order[least] {
			least = d.AdmissionPhase
		}
	}
	return least
}

func (b *Backend) clusterIdentity(id string) (string, *string) {
	for _, c := range b.state.Clusters {
		if c.ID == id {
			region := c.Region
			return c.Name, &region
		}
	}
	return id, nil
}

// CreateWorkload expands the workload the way placement would, which for the
// fake means the pinned cluster or the first placeable one: `pack` puts
// everything in one cluster whenever one cluster can hold it, and that is the
// default precisely so a workload that fits stays where it is.
func (b *Backend) CreateWorkload(ctx context.Context, req api.WorkloadCreateRequest) (api.Workload, error) {
	if strings.TrimSpace(req.Name) == "" {
		return api.Workload{}, errs.New(errs.CodeInputRequired, "a workload needs a name")
	}
	artifact, err := b.Artifact(ctx, req.Serving.Artifact)
	if err != nil {
		return api.Workload{}, err
	}
	project, err := b.Project(ctx, req.Project)
	if err != nil {
		return api.Workload{}, err
	}
	for _, wl := range b.state.Workloads {
		if wl.ProjectID == project.ID && strings.EqualFold(wl.Name, req.Name) {
			return api.Workload{}, errs.New(errs.CodeConflict, "%q already exists in this project", req.Name)
		}
	}
	kind := orElse(req.Kind, "service")
	if kind == "gang" && req.MaxClusters != nil && *req.MaxClusters > 1 {
		return api.Workload{}, errs.New(errs.CodeUsage,
			"a gang cannot be split across clusters, so --max-clusters above 1 is not a thing it can do")
	}

	target := ""
	if req.Cluster != "" {
		c, err := b.Cluster(ctx, req.Cluster)
		if err != nil {
			return api.Workload{}, err
		}
		target = c.ID
	} else if len(b.state.Clusters) > 0 {
		target = b.state.Clusters[0].ID
	}

	now := time.Now().UTC()
	serving := normalizeServing(servingOf(artifact.ID, req.Serving))
	floors := map[string]any{}
	for k, v := range req.ClusterFloors {
		floors[k] = v
	}
	pinned := optional(target)
	if req.Cluster == "" {
		pinned = nil
	}
	workload := api.Workload{
		ID: b.newUID("wkl_"), Name: req.Name, Kind: kind, ProjectID: project.ID,
		PriorityClass: orElse(req.PriorityClass, "production"),
		Preemptible:   req.Preemptible, GangSize: max(req.GangSize, 1),
		Serving:            serving,
		ServingDigest:      servingDigest(serving),
		AdapterArtifactIDs: req.AdapterArtifacts,
		Overrides:          req.Overrides, Labels: req.Labels,
		Strategy:      orElse(req.Strategy, "pack"),
		ClusterFloors: floors, MaxClusters: req.MaxClusters, PinnedClusterID: pinned,
		Scale: api.WorkloadScale{
			Unit: scaleUnit(kind), Min: req.MinScale, Max: req.MaxScale, Desired: req.DesiredScale,
		},
		DeadlineAt: req.DeadlineAt, CreatedAt: now, Revision: revision(now),
		Deployments: []api.WorkloadDeployment{},
	}
	if workload.AdapterArtifactIDs == nil {
		workload.AdapterArtifactIDs = []string{}
	}
	if workload.Overrides == nil {
		workload.Overrides = map[string]any{}
	}
	if workload.Labels == nil {
		workload.Labels = map[string]any{}
	}
	b.state.Workloads = append(b.state.Workloads, workload)

	if target != "" {
		b.state.Deployments = append(b.state.Deployments, api.Deployment{
			ID: b.newUID("dep_"), Name: workload.Name, WorkloadID: workload.ID,
			WorkloadName: workload.Name, ClusterID: target,
			MinReplicas: req.DesiredScale, MaxReplicas: req.DesiredScale, DesiredReplicas: req.DesiredScale,
			AdmissionPhase: "pending", QueuedAt: &now,
			Generation: 1, Enabled: req.Enabled, Authority: "managed",
			CreatedAt: now, Revision: revision(now),
			Replicas: api.DeploymentReplicaRollup{},
		})
	}
	if err := b.persist(); err != nil {
		return api.Workload{}, err
	}
	return b.hydrateWorkload(workload), nil
}

// UpdateWorkload writes only what was sent. Scale is absent on purpose: it is
// the workload.scale operation, because it is long-running and can fail.
func (b *Backend) UpdateWorkload(ctx context.Context, ref string, req api.WorkloadUpdateRequest) (api.Workload, error) {
	found, err := b.Workload(ctx, ref)
	if err != nil {
		return api.Workload{}, err
	}
	for i := range b.state.Workloads {
		wl := &b.state.Workloads[i]
		if wl.ID != found.ID {
			continue
		}
		if req.Name != nil {
			wl.Name = *req.Name
		}
		if req.AdapterArtifacts != nil {
			wl.AdapterArtifactIDs = *req.AdapterArtifacts
		}
		if req.Overrides != nil {
			wl.Overrides = *req.Overrides
		}
		if req.Labels != nil {
			wl.Labels = *req.Labels
		}
		if req.PriorityClass != nil {
			wl.PriorityClass = *req.PriorityClass
		}
		if req.Preemptible != nil {
			wl.Preemptible = *req.Preemptible
		}
		if req.Strategy != nil {
			wl.Strategy = *req.Strategy
		}
		if req.ClusterFloors != nil {
			floors := map[string]any{}
			for k, v := range *req.ClusterFloors {
				floors[k] = v
			}
			wl.ClusterFloors = floors
		}
		if req.MaxClusters != nil {
			wl.MaxClusters = req.MaxClusters
		}
		now := time.Now().UTC()
		wl.UpdatedAt = &now
		wl.Revision = revision(now)
		if err := b.persist(); err != nil {
			return api.Workload{}, err
		}
		return b.hydrateWorkload(*wl), nil
	}
	return api.Workload{}, errs.New(errs.CodeNotFound, "no workload %q", ref)
}

// DeleteWorkload takes the workload and every cluster's share of it. Removing
// one cluster is workload.drain; there is no delete on a deployment.
func (b *Backend) DeleteWorkload(ctx context.Context, ref string) error {
	found, err := b.Workload(ctx, ref)
	if err != nil {
		return err
	}
	workloads := make([]api.Workload, 0, len(b.state.Workloads))
	for _, wl := range b.state.Workloads {
		if wl.ID != found.ID {
			workloads = append(workloads, wl)
		}
	}
	deployments := make([]api.Deployment, 0, len(b.state.Deployments))
	for _, d := range b.state.Deployments {
		if d.WorkloadID != found.ID {
			deployments = append(deployments, d)
		}
	}
	b.state.Workloads = workloads
	b.state.Deployments = deployments
	return b.persist()
}

func (b *Backend) Deployments(ctx context.Context, o api.ListOpts) (api.Page[api.Deployment], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Deployments, o, func(d api.Deployment) bool {
		return refEq(cluster, d.ClusterID) &&
			refEq(o.Workload, d.WorkloadID, d.WorkloadName) &&
			refEq(o.Project, b.workloadProject(d.WorkloadID)) &&
			eqFold(o.Phase, d.AdmissionPhase)
	})
}

func (b *Backend) workloadProject(workloadID string) string {
	for _, wl := range b.state.Workloads {
		if wl.ID == workloadID {
			return wl.ProjectID
		}
	}
	return ""
}

func (b *Backend) Deployment(ctx context.Context, ref string) (api.Deployment, error) {
	return find(b, "deployment", b.state.Deployments, ref, func(d api.Deployment) []string {
		return []string{d.Name, d.ID}
	})
}

// UpdateDeployment writes the one per-cluster field a caller owns. Everything a
// workload declares is declared once, globally, and a share is placement's
// answer rather than something asserted per cluster.
func (b *Backend) UpdateDeployment(ctx context.Context, ref string, req api.DeploymentUpdateRequest) (api.Deployment, error) {
	found, err := b.Deployment(ctx, ref)
	if err != nil {
		return api.Deployment{}, err
	}
	for i := range b.state.Deployments {
		d := &b.state.Deployments[i]
		if d.ID != found.ID {
			continue
		}
		if req.Enabled != nil {
			d.Enabled = *req.Enabled
		}
		now := time.Now().UTC()
		d.UpdatedAt = &now
		d.Revision = revision(now)
		if err := b.persist(); err != nil {
			return api.Deployment{}, err
		}
		return *d, nil
	}
	return api.Deployment{}, errs.New(errs.CodeNotFound, "no deployment %q", ref)
}

func (b *Backend) DiscoveredWorkloads(ctx context.Context, o api.ListOpts) (api.Page[api.DiscoveredWorkload], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Discovered, o, func(d api.DiscoveredWorkload) bool {
		return refEq(cluster, d.ClusterID) && eqFold(o.Namespace, d.Namespace) &&
			eqFold(o.Status, d.Status) && contains(o.Search, d.Name, d.ID)
	})
}

func (b *Backend) DiscoveredWorkload(ctx context.Context, ref string) (api.DiscoveredWorkload, error) {
	return find(b, "discovered workload", b.state.Discovered, ref, func(d api.DiscoveredWorkload) []string {
		return []string{d.Name, d.ID}
	})
}

// DecideWorkload records the decision and touches nothing else. The fake is as
// inert as the real thing here on purpose: if adopting could ever produce a
// declared object, this is where the shape test would catch it.
func (b *Backend) DecideWorkload(ctx context.Context, ref, decision string, req api.DecisionRequest) (api.DiscoveredWorkload, error) {
	found, err := b.DiscoveredWorkload(ctx, ref)
	if err != nil {
		return api.DiscoveredWorkload{}, err
	}
	now := time.Now().UTC()
	for i := range b.state.Discovered {
		if b.state.Discovered[i].ID != found.ID {
			continue
		}
		d := &b.state.Discovered[i]
		d.Status = decided(decision)
		d.DecidedAt = &now
		who := "admin"
		d.DecidedBy = &who
		d.ProjectID = optional(req.Project)
		d.Note = optional(req.Note)
		if err := b.persist(); err != nil {
			return api.DiscoveredWorkload{}, err
		}
		return *d, nil
	}
	return api.DiscoveredWorkload{}, errs.New(errs.CodeNotFound, "no discovered workload %q", ref)
}

// decided turns the verb the caller used into the state it leaves behind, which
// is what the plane stores and what every reader compares against.
func decided(decision string) string {
	switch decision {
	case "ignore":
		return "ignored"
	case "remove":
		return "dismissed"
	default:
		return "tracked"
	}
}

// RemoveWorkload records the removal. Nothing is deleted here either, for the reason nothing is
// deleted in the plane: the sighting is observed, and a fake that removed the row would model a
// removal the real thing cannot perform.
func (b *Backend) RemoveWorkload(ctx context.Context, ref string) error {
	_, err := b.DecideWorkload(ctx, ref, "remove", api.DecisionRequest{})
	return err
}

// RestoreWorkload drops the decision, leaving the sighting as though nobody had looked at it.
func (b *Backend) RestoreWorkload(ctx context.Context, ref string) (api.DiscoveredWorkload, error) {
	found, err := b.DiscoveredWorkload(ctx, ref)
	if err != nil {
		return api.DiscoveredWorkload{}, err
	}
	for i := range b.state.Discovered {
		d := &b.state.Discovered[i]
		if d.ID != found.ID {
			continue
		}
		if d.Status != "dismissed" {
			return api.DiscoveredWorkload{}, errs.New(errs.CodeInvalidState, "%q was not removed", d.Name)
		}
		d.Status = "new"
		d.DecidedAt, d.DecidedBy, d.Note = nil, nil, nil
		if err := b.persist(); err != nil {
			return api.DiscoveredWorkload{}, err
		}
		return *d, nil
	}
	return api.DiscoveredWorkload{}, errs.New(errs.CodeNotFound, "no discovered workload %q", ref)
}

// PromoteWorkload mirrors the plane's gate, refusals included. A fake that promoted anything it
// was asked to would let a command that cannot work in production pass its tests.
//
// It mints the pair: a workload pinned to the one cluster the sighting runs in, and the single
// deployment that is that cluster's copy. The scale is what the workload is already running, so
// cataloguing it cannot scale it as a side effect.
func (b *Backend) PromoteWorkload(ctx context.Context, ref string, req api.PromoteRequest) (api.Workload, error) {
	found, err := b.DiscoveredWorkload(ctx, ref)
	if err != nil {
		return api.Workload{}, err
	}
	if found.DeploymentID != nil {
		return api.Workload{}, errs.New(errs.CodeConflict, "%q was already promoted", found.Name)
	}
	if found.SpecFingerprint == nil || *found.SpecFingerprint == "" {
		return api.Workload{}, errs.New(errs.CodeInvalidState,
			"this cluster has not reported what %q is running, so there is nothing to pin the adoption to", found.Name)
	}
	if req.Fingerprint != *found.SpecFingerprint {
		return api.Workload{}, errs.New(errs.CodeConflict,
			"%q changed since it was read; re-read it and promote again", found.Name)
	}
	artifact, err := b.Artifact(ctx, req.Artifact)
	if err != nil {
		return api.Workload{}, err
	}
	project, err := b.Project(ctx, req.Project)
	if err != nil {
		return api.Workload{}, err
	}

	now := time.Now().UTC()
	// The serving configuration is read off what is running, never chosen here:
	// a promotion that picked a shape would be a promotion that could restart
	// the thing it is only cataloguing.
	serving := normalizeServing(api.ServingSpec{
		ArtifactID: artifact.ID,
		Shape:      api.ServingShape{DeviceClass: "unknown", DeviceCount: found.DevicesPerReplica},
	})
	if len(found.Images) > 0 {
		serving.EngineRuntimeImage = &found.Images[0]
	}
	name := req.Name
	if name == "" {
		name = found.Name
	}
	workload := api.Workload{
		ID: b.newUID("wkl_"), Name: name, Kind: "service", ProjectID: project.ID,
		PriorityClass:      orElse(req.PriorityClass, "production"),
		GangSize:           1,
		Serving:            serving,
		ServingDigest:      servingDigest(serving),
		AdapterArtifactIDs: []string{},
		Overrides:          map[string]any{},
		Labels:             found.Labels,
		Strategy:           "pack",
		ClusterFloors:      map[string]any{},
		PinnedClusterID:    &found.ClusterID,
		Scale: api.WorkloadScale{
			Unit: "replicas", Min: found.ReplicasDesired, Max: found.ReplicasDesired,
			Desired: found.ReplicasDesired,
		},
		StartedAt: &now, CreatedAt: now, Revision: revision(now),
		Deployments: []api.WorkloadDeployment{},
	}
	deployment := api.Deployment{
		ID: b.newUID("dep_"), Name: name, WorkloadID: workload.ID, WorkloadName: workload.Name,
		ClusterID:       found.ClusterID,
		MinReplicas:     found.ReplicasDesired,
		MaxReplicas:     found.ReplicasDesired,
		DesiredReplicas: found.ReplicasDesired,
		AdmissionPhase:  "running",
		QueuedAt:        &now, AdmittedAt: &now, PlacedAt: &now,
		Generation: 1, Enabled: true, Authority: "observed",
		CreatedAt:           now,
		Revision:            revision(now),
		AdoptedDiscoveredID: &found.ID,
		AdoptedFingerprint:  found.SpecFingerprint,
		Replicas: api.DeploymentReplicaRollup{
			Total: found.ReplicasDesired, Ready: found.ReplicasReady, Running: found.ReplicasReady,
			AtCurrentGeneration: found.ReplicasReady,
		},
	}
	b.state.Workloads = append(b.state.Workloads, workload)
	b.state.Deployments = append(b.state.Deployments, deployment)
	for i := range b.state.Discovered {
		if b.state.Discovered[i].ID == found.ID {
			b.state.Discovered[i].Status = "promoted"
			b.state.Discovered[i].DeploymentID = &deployment.ID
			b.state.Discovered[i].ProjectID = &project.ID
		}
	}
	if err := b.persist(); err != nil {
		return api.Workload{}, err
	}
	return b.hydrateWorkload(workload), nil
}

func orElse(v, fallback string) string {
	if strings.TrimSpace(v) == "" {
		return fallback
	}
	return v
}

func optional(v string) *string {
	if strings.TrimSpace(v) == "" {
		return nil
	}
	return &v
}

func (b *Backend) Endpoints(ctx context.Context, o api.ListOpts) (api.Page[api.Endpoint], error) {
	return collect(b, b.state.Endpoints, o, func(e api.Endpoint) bool {
		return refEq(o.Project, e.ProjectID) &&
			contains(o.Search, e.Name, e.ID, e.Slug) &&
			labelsMatch(e.Labels, o.Labels)
	})
}

func (b *Backend) Endpoint(ctx context.Context, ref string) (api.Endpoint, error) {
	return find(b, "endpoint", b.state.Endpoints, ref, func(e api.Endpoint) []string {
		return []string{e.Name, e.ID}
	})
}

func (b *Backend) Tenants(ctx context.Context, o api.ListOpts) (api.Page[api.Tenant], error) {
	return collect(b, b.state.Tenants, o, func(t api.Tenant) bool {
		return boolEq(o.Enabled, t.Enabled) && contains(o.Search, t.Slug, t.Name, t.ID)
	})
}

func (b *Backend) Tenant(ctx context.Context, ref string) (api.Tenant, error) {
	return find(b, "tenant", b.state.Tenants, ref, func(t api.Tenant) []string {
		return []string{t.Slug, t.ID}
	})
}

func (b *Backend) Projects(ctx context.Context, o api.ListOpts) (api.Page[api.Project], error) {
	return collect(b, b.state.Projects, o, func(p api.Project) bool {
		return refEq(o.Tenant, p.Tenant.ID, p.Tenant.Slug) && boolEq(o.Enabled, p.Enabled) &&
			contains(o.Search, p.Slug, p.Name, p.ID)
	})
}

func (b *Backend) Project(ctx context.Context, ref string) (api.Project, error) {
	return find(b, "project", b.state.Projects, ref, func(p api.Project) []string {
		return []string{p.Slug, p.ID}
	})
}

func (b *Backend) Quotas(ctx context.Context, o api.ListOpts) (api.Page[api.Quota], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Quotas, o, func(q api.Quota) bool {
		return refEq(o.Project, q.Project, q.ProjectSlug) &&
			refEqPtr(cluster, q.Cluster, q.ClusterName) &&
			eqFold(o.Class, q.DeviceClass) && boolEq(o.OverQuotaEnabled, q.OverQuotaEnabled)
	})
}

func (b *Backend) Quota(ctx context.Context, ref string) (api.Quota, error) {
	return find(b, "quota", b.state.Quotas, ref, func(q api.Quota) []string {
		return []string{q.ID}
	})
}

// ServingQuotas is its own collection because a serving quota is its own
// object: a device quota is checked once by placement, this is checked on every
// request by the gateway, and they share a word and nothing else.
func (b *Backend) ServingQuotas(ctx context.Context, o api.ListOpts) (api.Page[api.ServingQuota], error) {
	endpoint := b.endpointRef(o.Endpoint)
	return collect(b, b.state.ServingQuotas, o, func(q api.ServingQuota) bool {
		return refEq(endpoint, q.Endpoint, q.EndpointName) && refEq(o.Project, q.Project) &&
			boolEq(o.Enforced, q.Enforced) && eqFold(o.Scope, q.LimitScope)
	})
}

func (b *Backend) ServingQuota(ctx context.Context, ref string) (api.ServingQuota, error) {
	return find(b, "serving-quota", b.state.ServingQuotas, ref, func(q api.ServingQuota) []string {
		return []string{q.ID, q.EndpointName}
	})
}

func (b *Backend) CreateServingQuota(ctx context.Context, req api.ServingQuotaCreate) (api.ServingQuota, error) {
	if err := b.injected(); err != nil {
		return api.ServingQuota{}, err
	}
	endpoint, err := b.Endpoint(ctx, req.Endpoint)
	if err != nil {
		return api.ServingQuota{}, err
	}
	for _, existing := range b.state.ServingQuotas {
		if existing.Endpoint == endpoint.ID {
			return api.ServingQuota{}, errs.New(errs.CodeConflict, "%s already carries a serving quota", endpoint.Name).
				WithFix("Edit the one it has", "hgr serving-quota set "+existing.ID+" --committed-rpm N")
		}
	}
	now := time.Now().UTC()
	quota := api.ServingQuota{
		ID:                     b.newUID("svq_"),
		Endpoint:               endpoint.ID,
		EndpointName:           endpoint.Name,
		Project:                endpoint.ProjectID,
		CommittedRPM:           req.CommittedRPM,
		CommittedTPM:           req.CommittedTPM,
		MaxConcurrentRequests:  req.MaxConcurrentRequests,
		MaxOutputTokens:        req.MaxOutputTokens,
		BurstRPM:               req.BurstRPM,
		RampUpSeconds:          intOr(req.RampUpSeconds, 0),
		OnExceed:               orElse(req.OnExceed, api.OnExceedReject),
		QueueTimeoutMS:         req.QueueTimeoutMS,
		LimitScope:             orElse(req.LimitScope, api.ScopePerReplica),
		SuccessRateObjective:   req.SuccessRateObjective,
		AvailabilityObjective:  orElse(req.AvailabilityObjective, defaultAvailabilityObjective),
		ObjectiveWindowSeconds: intOr(req.ObjectiveWindowSeconds, 600),
		Enforced:               req.Enforced != nil && *req.Enforced,
		CreatedAt:              now,
		Revision:               revision(now),
	}
	if err := checkServingQuota(quota); err != nil {
		return api.ServingQuota{}, err
	}
	quota.Standard = standardOf(quota)
	b.state.ServingQuotas = append(b.state.ServingQuotas, quota)
	return quota, b.persist()
}

func (b *Backend) UpdateServingQuota(ctx context.Context, ref string, req api.ServingQuotaUpdate) (api.ServingQuota, error) {
	quota, err := b.ServingQuota(ctx, ref)
	if err != nil {
		return quota, err
	}
	if req.CommittedRPM != nil {
		quota.CommittedRPM = req.CommittedRPM
	}
	if req.CommittedTPM != nil {
		quota.CommittedTPM = req.CommittedTPM
	}
	if req.MaxConcurrentRequests != nil {
		quota.MaxConcurrentRequests = req.MaxConcurrentRequests
	}
	if req.MaxOutputTokens != nil {
		quota.MaxOutputTokens = req.MaxOutputTokens
	}
	if req.BurstRPM != nil {
		quota.BurstRPM = req.BurstRPM
	}
	if req.RampUpSeconds != nil {
		quota.RampUpSeconds = *req.RampUpSeconds
	}
	if req.OnExceed != "" {
		quota.OnExceed = req.OnExceed
	}
	if req.QueueTimeoutMS != nil {
		quota.QueueTimeoutMS = req.QueueTimeoutMS
	}
	if req.LimitScope != "" {
		quota.LimitScope = req.LimitScope
	}
	if req.SuccessRateObjective != nil {
		quota.SuccessRateObjective = req.SuccessRateObjective
	}
	if req.AvailabilityObjective != "" {
		quota.AvailabilityObjective = req.AvailabilityObjective
	}
	if req.ObjectiveWindowSeconds != nil {
		quota.ObjectiveWindowSeconds = *req.ObjectiveWindowSeconds
	}
	if req.Enforced != nil {
		quota.Enforced = *req.Enforced
	}
	if err := checkServingQuota(quota); err != nil {
		return api.ServingQuota{}, err
	}
	now := time.Now().UTC()
	quota.UpdatedAt = &now
	quota.Revision = revision(now)
	quota.Standard = standardOf(quota)
	for i, existing := range b.state.ServingQuotas {
		if existing.ID == quota.ID {
			b.state.ServingQuotas[i] = quota
		}
	}
	return quota, b.persist()
}

func (b *Backend) DeleteServingQuota(ctx context.Context, ref string) error {
	quota, err := b.ServingQuota(ctx, ref)
	if err != nil {
		return err
	}
	kept := make([]api.ServingQuota, 0, len(b.state.ServingQuotas))
	for _, existing := range b.state.ServingQuotas {
		if existing.ID != quota.ID {
			kept = append(kept, existing)
		}
	}
	b.state.ServingQuotas = kept
	return b.persist()
}

// ResolvedServingQuotas applies limit_scope to a fleet of a stated size. The
// count comes from the caller here for the same reason it does on the plane:
// nothing in a control plane may discover it by dialling a cluster.
func (b *Backend) ResolvedServingQuotas(ctx context.Context, endpoint, workload string, replicas, index int) (api.ResolvedServingQuotaTable, error) {
	if err := b.injected(); err != nil {
		return api.ResolvedServingQuotaTable{}, err
	}
	if replicas < 1 {
		return api.ResolvedServingQuotaTable{}, errs.New(errs.CodeUsage, "replicas must be at least 1")
	}
	if index < 0 || index >= replicas {
		return api.ResolvedServingQuotaTable{}, errs.New(errs.CodeUsage, "replica_index %d is outside a fleet of %d", index, replicas)
	}
	ref := b.endpointRef(endpoint)
	table := api.ResolvedServingQuotaTable{
		Revision: revision(time.Now().UTC()), Replicas: replicas, ReplicaIndex: index,
		Quotas: []api.ResolvedServingQuota{},
	}
	for _, quota := range b.state.ServingQuotas {
		if !refEq(ref, quota.Endpoint, quota.EndpointName) {
			continue
		}
		workloads := b.workloadsBehind(quota.Endpoint)
		if workload != "" && !refEqAny(workload, workloads, workloads) {
			continue
		}
		table.Quotas = append(table.Quotas, api.ResolvedServingQuota{
			Endpoint: quota.Endpoint, EndpointName: quota.EndpointName, Project: quota.Project,
			Workloads: workloads, Replicas: replicas, ReplicaIndex: index,
			Declared:               declaredLimits(quota),
			Effective:              effectiveLimits(quota, replicas, index),
			StarvedReplicas:        starvedReplicas(quota, replicas),
			SuccessRateObjective:   quota.SuccessRateObjective,
			AvailabilityObjective:  quota.AvailabilityObjective,
			ObjectiveWindowSeconds: quota.ObjectiveWindowSeconds,
			Standard:               quota.Standard,
			Revision:               quota.Revision,
		})
	}
	return table, nil
}

// workloadsBehind is a lookup convenience and never a key: the commitment
// belongs to the endpoint, and a workload appears because traffic reaching it
// was admitted through this endpoint's name.
func (b *Backend) workloadsBehind(endpoint string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, e := range b.state.Endpoints {
		if e.ID != endpoint {
			continue
		}
		for _, target := range e.Targets {
			for _, d := range b.state.Deployments {
				if d.ID == target.DeploymentID && !seen[d.WorkloadID] {
					seen[d.WorkloadID] = true
					out = append(out, d.WorkloadID)
				}
			}
		}
	}
	return out
}

func (b *Backend) endpointRef(ref string) string {
	if ref == "" {
		return ""
	}
	if e, err := b.Endpoint(context.Background(), ref); err == nil {
		return e.ID
	}
	return ref
}

func (b *Backend) PriorityClasses(ctx context.Context, o api.ListOpts) (api.Page[api.PriorityClass], error) {
	return collect(b, b.state.PriorityClasses, o, func(p api.PriorityClass) bool {
		return boolEq(o.Preemptible, p.Preemptible) && boolEq(o.Preempts, p.Preempts) &&
			contains(o.Search, p.Name, p.ID)
	})
}

func (b *Backend) PriorityClass(ctx context.Context, ref string) (api.PriorityClass, error) {
	return find(b, "priority-class", b.state.PriorityClasses, ref, func(p api.PriorityClass) []string {
		return []string{p.Name, p.ID}
	})
}

func (b *Backend) Policies(ctx context.Context, o api.ListOpts) (api.Page[api.Policy], error) {
	return collect(b, b.state.Policies, o, func(p api.Policy) bool {
		return boolEq(o.Enabled, p.Enabled) &&
			listHas(p.AllowedRegions, o.Region) &&
			listHas(p.AllowedDeviceClasses, o.Class) &&
			contains(o.Search, p.Name, p.ID) &&
			policyBinds(p, o.Kind, o.Subject)
	})
}

func (b *Backend) Policy(ctx context.Context, ref string) (api.Policy, error) {
	return find(b, "policy", b.state.Policies, ref, func(p api.Policy) []string {
		return []string{p.Name, p.ID}
	})
}

func (b *Backend) Allocations(ctx context.Context, o api.ListOpts) (api.Page[api.Allocation], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Allocations, o, func(a api.Allocation) bool {
		return refEq(cluster, a.Cluster, a.ClusterName) &&
			refEq(o.Project, a.Project, a.ProjectSlug) &&
			eqFold(o.Class, a.DeviceClass) && eqFold(o.Phase, a.Phase) &&
			boolEq(o.OverQuota, a.OverQuota) && boolEq(o.Live, a.Live) &&
			refEqPtr(o.Replica, a.Replica, nil) &&
			allocationHolds(a, o.Device)
	})
}

func (b *Backend) Allocation(ctx context.Context, ref string) (api.Allocation, error) {
	return find(b, "allocation", b.state.Allocations, ref, func(a api.Allocation) []string {
		return []string{a.ID}
	})
}

func (b *Backend) Operations(ctx context.Context, o api.ListOpts) (api.Page[api.Operation], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Operations, o, func(op api.Operation) bool {
		return refEqPtr(cluster, op.ClusterID, nil) &&
			eqFold(o.Type, op.Type) && eqFold(o.Phase, op.Phase) &&
			refEq(o.Target, op.Target.ID) && eqFold(o.Actor, op.RequestedBy) &&
			(o.Since == nil || !op.CreatedAt.Before(*o.Since))
	})
}

// Operation advances a running operation one step per poll, so that waiting
// on a drain is exercisable offline: an operation that never moves would make
// `hgr operation wait` look correct while it is broken.
func (b *Backend) Operation(ctx context.Context, ref string) (api.Operation, error) {
	op, err := find(b, "operation", b.state.Operations, ref, func(op api.Operation) []string {
		return []string{op.ID}
	})
	if err != nil {
		return op, err
	}
	b.polls[op.ID]++
	advanced := b.advance(op)
	return advanced, b.persist()
}

func (b *Backend) advance(op api.Operation) api.Operation {
	if op.Terminal() {
		return op
	}
	now := time.Now().UTC()
	switch {
	case b.polls[op.ID] < 2:
		return op
	case op.CancelRequestedAt != nil:
		op.Phase = api.OpCancelled
		op.FinishedAt = &now
	case b.polls[op.ID] < 3:
		op.Phase = api.OpRunning
		op.StartedAt = &now
		op.Attempts = 1
	default:
		op.Phase = api.OpSucceeded
		op.StartedAt = &now
		op.FinishedAt = &now
		op.Attempts = 1
		op.Result = map[string]any{"evicted_replicas": 3}
	}
	b.replaceOperation(op)
	return op
}

func (b *Backend) replaceOperation(op api.Operation) {
	for i, existing := range b.state.Operations {
		if existing.ID == op.ID {
			b.state.Operations[i] = op
			return
		}
	}
	b.state.Operations = append([]api.Operation{op}, b.state.Operations...)
}

// fakeHarnesses mirrors what the plane serves at /v1/benchmarks/harnesses.
//
// A copy, and knowingly one: the fake exists to answer offline, and there is no
// plane to ask. Drift between the two shows up as a flag the real plane refuses,
// which is a bug in the fake and not in the design.
var fakeHarnesses = []api.BenchmarkHarness{{
	Name:        "aiperf",
	Image:       fakeAiperfImage,
	Description: "NVIDIA AIPerf, the successor to GenAI-Perf. Closed-loop, concurrency-first.",
	Parameters: []api.BenchmarkParameter{
		{Name: "concurrency", Type: "int", Default: 10, Description: "Requests in flight at once."},
		{Name: "request_count", Type: "int", Default: 100, Description: "Total requests to send."},
		{Name: "input_tokens_mean", Type: "int", Default: 2000, Description: "Mean synthetic prompt length, in tokens."},
		{Name: "input_tokens_stddev", Type: "int", Default: 0, Description: "Spread of the prompt length."},
		{Name: "output_tokens_mean", Type: "int", Default: 256, Description: "Mean generation length, in tokens."},
		{Name: "output_tokens_stddev", Type: "int", Default: 0, Description: "Spread of the generation length."},
		{Name: "endpoint_type", Type: "str", Default: "chat", Description: "AIPerf endpoint type: chat, completions, embeddings."},
		{Name: "streaming", Type: "bool", Default: true, Description: "Measure TTFT and ITL, which needs a streamed response."},
		{Name: "warmup_request_count", Type: "int", Default: 10, Description: "Requests sent and discarded before measuring."},
		{Name: "extra", Type: "str", Default: "", Description: "Extra flags, appended verbatim to the command line."},
	},
}}

func (b *Backend) Benchmarks(ctx context.Context, o api.ListOpts) (api.Page[api.BenchmarkRun], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Benchmarks, o, func(run api.BenchmarkRun) bool {
		return refEqPtr(cluster, run.ClusterID, nil) &&
			refEqPtr(o.Deployment, run.DeploymentID, nil) && eqFold(o.Harness, run.Harness) &&
			eqFold(o.Phase, run.Phase) && eqFold(o.Actor, run.RequestedBy) &&
			refEqPtr(o.Request, run.RequestID, nil) &&
			(o.Since == nil || !run.CreatedAt.Before(*o.Since))
	})
}

func (b *Backend) Benchmark(ctx context.Context, ref string) (api.BenchmarkRun, error) {
	return find(b, "benchmark run", b.state.Benchmarks, ref, func(run api.BenchmarkRun) []string {
		return []string{run.ID}
	})
}

func (b *Backend) BenchmarkHarnesses(ctx context.Context) ([]api.BenchmarkHarness, error) {
	if err := b.injected(); err != nil {
		return nil, err
	}
	return fakeHarnesses, nil
}

// CreateBenchmark refuses the same things the plane does: an unknown harness and
// a parameter it does not accept. A fake that accepted everything would teach a
// command line that the plane then rejects.
func (b *Backend) CreateBenchmark(ctx context.Context, req api.BenchmarkRunCreate) (api.BenchmarkRun, error) {
	if err := b.injected(); err != nil {
		return api.BenchmarkRun{}, err
	}
	if strings.TrimSpace(req.Deployment) == "" {
		return api.BenchmarkRun{}, errs.New(errs.CodeInputRequired, "a benchmark needs a deployment")
	}
	harness, parameters, err := b.resolveHarness(req.Harness, req.Parameters)
	if err != nil {
		return api.BenchmarkRun{}, err
	}
	deployment, err := b.Deployment(ctx, req.Deployment)
	if err != nil {
		return api.BenchmarkRun{}, err
	}
	deploymentID, clusterID := deployment.ID, deployment.ClusterID
	run := api.BenchmarkRun{
		ID:           b.newUID("bmk_"),
		DeploymentID: &deploymentID,
		ClusterID:    &clusterID,
		Harness:      harness.Name,
		Image:        orElse(req.Image, harness.Image),
		Parameters:   parameters,
		RequestedBy:  "operator",
		Provenance: api.BenchmarkProvenance{
			Model:                deployment.Name,
			DeploymentGeneration: int64(deployment.Generation),
			EngineArgs:           map[string]any{},
		},
		Phase:     api.OpPending,
		CreatedAt: time.Now().UTC(),
	}
	if req.Reason != "" {
		reason := req.Reason
		run.Reason = &reason
	}
	b.state.Benchmarks = append([]api.BenchmarkRun{run}, b.state.Benchmarks...)
	return run, b.persist()
}

// resolveHarness refuses the same things the plane does — an unknown harness and
// a parameter it does not accept — and returns the harness with its defaults
// laid under the parameters the caller named. A fake that accepted everything
// would teach a command line the plane then rejects.
func (b *Backend) resolveHarness(name string, params map[string]any) (*api.BenchmarkHarness, map[string]any, error) {
	var harness *api.BenchmarkHarness
	for i := range fakeHarnesses {
		if eqFold(name, fakeHarnesses[i].Name) || name == "" {
			harness = &fakeHarnesses[i]
			break
		}
	}
	if harness == nil {
		return nil, nil, errs.New(errs.CodeUsage, "unknown benchmark harness %q", name)
	}
	known := map[string]bool{}
	for _, parameter := range harness.Parameters {
		known[parameter.Name] = true
	}
	for pname := range params {
		if !known[pname] {
			return nil, nil, errs.New(errs.CodeUsage, "%s does not accept %q", harness.Name, pname)
		}
	}
	parameters := map[string]any{}
	for _, parameter := range harness.Parameters {
		parameters[parameter.Name] = parameter.Default
	}
	for pname, value := range params {
		parameters[pname] = value
	}
	return harness, parameters, nil
}

// BenchmarkVerdict grades one run, now, against a pinned criteria version.
// Nothing is stored: the grading is derived on read from the run's summary and
// its artifact's parameter count, the same way the plane derives it.
func (b *Backend) BenchmarkVerdict(ctx context.Context, ref, criteria string) (api.BenchmarkVerdict, error) {
	run, err := b.Benchmark(ctx, ref)
	if err != nil {
		return api.BenchmarkVerdict{}, err
	}
	return verdictFor(run, b.parameterCountOf(run), nil, criteria), nil
}

// parameterCountOf reads the run's artifact through the serving digest its
// provenance names, so a run above the large-model threshold grades per service
// tier as the plane does.
func (b *Backend) parameterCountOf(run api.BenchmarkRun) *int64 {
	if run.Provenance.ServingDigest == nil {
		return nil
	}
	for _, wl := range b.state.Workloads {
		if wl.ServingDigest != *run.Provenance.ServingDigest {
			continue
		}
		for _, artifact := range b.state.Artifacts {
			if artifact.ID == wl.Serving.ArtifactID {
				return artifact.ParameterCount
			}
		}
	}
	return nil
}

func (b *Backend) BenchmarkRequest(ctx context.Context, ref string) (api.BenchmarkRequest, error) {
	return find(b, "benchmark request", b.state.BenchmarkAsks, ref, func(r api.BenchmarkRequest) []string {
		return []string{r.ID}
	})
}

// CreateBenchmarkRequest expands one ask into its runs, mirroring the plane: a
// stepped mode walks its load points and each step is one run, so a sweep is one
// request and several runs rather than several requests.
func (b *Backend) CreateBenchmarkRequest(ctx context.Context, req api.BenchmarkRequestCreate) (api.BenchmarkRequest, error) {
	if err := b.injected(); err != nil {
		return api.BenchmarkRequest{}, err
	}
	if strings.TrimSpace(req.Target) == "" {
		return api.BenchmarkRequest{}, errs.New(errs.CodeInputRequired, "a benchmark request needs a target")
	}
	level := orElse(req.Level, api.LevelDeployment)
	switch level {
	case api.LevelDeployment, api.LevelWorkload, api.LevelEndpoint:
	default:
		return api.BenchmarkRequest{}, errs.New(errs.CodeUsage, "unknown level %q", level).
			WithCause("deployment, workload or endpoint")
	}
	harness, baseParams, err := b.resolveHarness(req.Harness, req.Parameters)
	if err != nil {
		return api.BenchmarkRequest{}, err
	}

	now := time.Now().UTC()
	ask := api.BenchmarkRequest{
		ID:              b.newUID("bmr_"),
		Level:           level,
		TargetID:        req.Target,
		Harness:         harness.Name,
		Image:           orElse(req.Image, harness.Image),
		Parameters:      baseParams,
		Mode:            orElse(req.Mode, api.ModeSingle),
		CriteriaVersion: orElse(req.Criteria, criteriaVersion),
		RequestedBy:     "operator",
		Phase:           api.OpPending,
		Runs:            []string{},
		Coverage:        []api.BenchmarkCoverage{},
		CreatedAt:       now,
	}
	if req.Tier != "" {
		tier := req.Tier
		ask.Tier = &tier
	}
	if req.Reason != "" {
		reason := req.Reason
		ask.Reason = &reason
	}

	steps := req.Steps
	if len(steps) == 0 {
		steps = []int{0}
	}
	for i, step := range steps {
		parameters := map[string]any{}
		for name, value := range baseParams {
			parameters[name] = value
		}
		if step > 0 {
			parameters["input_tokens_mean"] = step
		}
		run := api.BenchmarkRun{
			ID:          b.newUID("bmk_"),
			RequestID:   &ask.ID,
			Harness:     harness.Name,
			Image:       ask.Image,
			Parameters:  parameters,
			StepIndex:   i,
			RequestedBy: "operator",
			Reason:      ask.Reason,
			Phase:       api.OpPending,
			CreatedAt:   now,
		}
		b.assignTarget(&run, level, req.Target)
		b.state.Benchmarks = append([]api.BenchmarkRun{run}, b.state.Benchmarks...)
		ask.Runs = append(ask.Runs, run.ID)
	}
	b.state.BenchmarkAsks = append([]api.BenchmarkRequest{ask}, b.state.BenchmarkAsks...)
	return ask, b.persist()
}

// assignTarget records which object a run measured, off the request's level.
// A deployment run carries a deployment and its cluster; an endpoint run carries
// neither, because a call to a public name is routed across every upstream.
func (b *Backend) assignTarget(run *api.BenchmarkRun, level, target string) {
	switch level {
	case api.LevelEndpoint:
		if e, err := b.Endpoint(context.Background(), target); err == nil {
			run.EndpointID = &e.ID
		}
	default:
		if d, err := b.Deployment(context.Background(), target); err == nil {
			id, cluster := d.ID, d.ClusterID
			run.DeploymentID = &id
			run.ClusterID = &cluster
		}
	}
}

// BenchmarkRequestVerdict rolls the request's runs up under one criteria
// version, which is a request's outcome: its runs' outcomes under the same rule.
func (b *Backend) BenchmarkRequestVerdict(ctx context.Context, ref, criteria string) (api.BenchmarkVerdict, error) {
	ask, err := b.BenchmarkRequest(ctx, ref)
	if err != nil {
		return api.BenchmarkVerdict{}, err
	}
	if criteria == "" {
		criteria = ask.CriteriaVersion
	}
	runs := b.runsOf(ask)
	entries := make([]api.BenchmarkVerdict, 0, len(runs))
	for _, run := range runs {
		entries = append(entries, verdictFor(run, b.parameterCountOf(run), ask.Tier, criteria))
	}
	return api.BenchmarkVerdict{
		Outcome: rollupVerdicts(entries), CriteriaVersion: criteria, Checks: []api.BenchmarkCheck{},
	}, nil
}

// BenchmarkRequestBands re-aggregates a request's runs by input-length band.
func (b *Backend) BenchmarkRequestBands(ctx context.Context, ref string) ([]api.BenchmarkBand, error) {
	ask, err := b.BenchmarkRequest(ctx, ref)
	if err != nil {
		return nil, err
	}
	return bandsOf(b.runsOf(ask)), nil
}

// runsOf collects the runs a request expanded into, in the order it named them.
func (b *Backend) runsOf(ask api.BenchmarkRequest) []api.BenchmarkRun {
	out := make([]api.BenchmarkRun, 0, len(ask.Runs))
	for _, id := range ask.Runs {
		for _, run := range b.state.Benchmarks {
			if run.ID == id {
				out = append(out, run)
				break
			}
		}
	}
	return out
}

// CreateOperation records an operation against a target the fake can resolve,
// so `--fake` refuses the same nonsense the plane would.
func (b *Backend) CreateOperation(ctx context.Context, req api.OperationRequest, idempotencyKey string) (api.Operation, error) {
	if err := b.injected(); err != nil {
		return api.Operation{}, err
	}
	if strings.TrimSpace(req.Type) == "" || strings.TrimSpace(req.Target) == "" {
		return api.Operation{}, errs.New(errs.CodeInputRequired, "an operation needs a type and a target")
	}
	if id, ok := b.state.OperationKeys[idempotencyKey]; ok && idempotencyKey != "" {
		for _, op := range b.state.Operations {
			if op.ID == id {
				return op, nil
			}
		}
	}
	target, cluster, err := b.resolveTarget(req)
	if err != nil {
		return api.Operation{}, err
	}
	now := time.Now().UTC()
	params := map[string]any{}
	for k, v := range req.Parameters {
		params[k] = v
	}
	op := api.Operation{
		ID:          b.newUID("opr_"),
		ClusterID:   cluster,
		Type:        req.Type,
		Target:      target,
		RequestedBy: "operator",
		Parameters:  params,
		DeadlineAt:  req.Deadline,
		CreatedAt:   now,
		Phase:       api.OpPending,
	}
	if req.Reason != "" {
		reason := req.Reason
		op.Reason = &reason
	}
	b.state.Operations = append([]api.Operation{op}, b.state.Operations...)
	if idempotencyKey != "" {
		if b.state.OperationKeys == nil {
			b.state.OperationKeys = map[string]string{}
		}
		b.state.OperationKeys[idempotencyKey] = op.ID
	}
	return op, b.persist()
}

// resolveTarget derives the target kind and the cluster from the target's own
// identity, because two ways to say which cluster is two ways to disagree.
// A `kind:name` reference is looked up by its name half: the kind is already
// implied by the operation type, and the plane checks the two agree.
func (b *Backend) resolveTarget(req api.OperationRequest) (api.OperationTarget, *string, error) {
	kind := strings.SplitN(req.Type, ".", 2)[0]
	req.Target = unqualifyTarget(req.Target)
	switch kind {
	case "node":
		n, err := b.Node(context.Background(), req.Target)
		if err != nil {
			return api.OperationTarget{}, nil, err
		}
		cluster := n.Cluster
		return api.OperationTarget{Type: "node", ID: n.ID}, &cluster, nil
	case "device":
		d, err := b.Device(context.Background(), req.Target)
		if err != nil {
			return api.OperationTarget{}, nil, err
		}
		cluster := d.Cluster
		return api.OperationTarget{Type: "device", ID: d.ID}, &cluster, nil
	case "cluster":
		c, err := b.Cluster(context.Background(), req.Target)
		if err != nil {
			return api.OperationTarget{}, nil, err
		}
		return api.OperationTarget{Type: "cluster", ID: c.ID}, &c.ID, nil
	case "deployment":
		d, err := b.Deployment(context.Background(), req.Target)
		if err != nil {
			return api.OperationTarget{}, nil, err
		}
		cluster := d.ClusterID
		return api.OperationTarget{Type: "deployment", ID: d.ID}, &cluster, nil
	case "workload":
		// No cluster. scale, pause and drain are plane-local: they rewrite the
		// expansion and let the shares reach the clusters as declared state,
		// rather than being dispatched to one.
		wl, err := b.Workload(context.Background(), req.Target)
		if err != nil {
			return api.OperationTarget{}, nil, err
		}
		return api.OperationTarget{Type: "workload", ID: wl.ID}, nil, nil
	}
	var cluster *string
	if req.Cluster != "" {
		c, err := b.Cluster(context.Background(), req.Cluster)
		if err != nil {
			return api.OperationTarget{}, nil, err
		}
		cluster = &c.ID
	}
	return api.OperationTarget{Type: kind, ID: req.Target}, cluster, nil
}

// unqualifyTarget strips the `kind:` half of a qualified target reference.
func unqualifyTarget(ref string) string {
	if _, name, ok := strings.Cut(ref, ":"); ok && name != "" {
		return name
	}
	return ref
}

// CancelOperation asks for cancellation. A pending operation stops outright;
// a running one is stamped and reaches its terminal phase when reported.
func (b *Backend) CancelOperation(ctx context.Context, ref string) (api.Operation, error) {
	op, err := find(b, "operation", b.state.Operations, ref, func(op api.Operation) []string {
		return []string{op.ID}
	})
	if err != nil {
		return op, err
	}
	if op.Terminal() {
		return op, errs.New(errs.CodeInvalidState, "operation %s already finished as %s", op.ID, op.Phase)
	}
	now := time.Now().UTC()
	op.CancelRequestedAt = &now
	if op.Phase == api.OpPending {
		op.Phase = api.OpCancelled
		op.FinishedAt = &now
	}
	b.replaceOperation(op)
	return op, b.persist()
}

func (b *Backend) Components(ctx context.Context, o api.ListOpts) (api.Page[api.Component], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Components, o, func(c api.Component) bool {
		return refEq(cluster, c.ClusterID) && eqFold(o.Name, c.Name) && eqFold(o.Kind, c.Kind) &&
			boolEq(o.Behind, c.Behind) && boolEq(o.Enabled, c.Enabled) &&
			contains(o.Search, c.Name, c.ID, c.Kind)
	})
}

func (b *Backend) Component(ctx context.Context, ref string) (api.Component, error) {
	return find(b, "component", b.state.Components, ref, func(c api.Component) []string {
		return []string{c.Name, c.ID}
	})
}

func (b *Backend) Events(ctx context.Context, o api.ListOpts) (api.Page[api.Event], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Events, o, func(e api.Event) bool {
		return refEqPtr(cluster, e.ClusterID, nil) &&
			refEqPtr(o.Subject, e.Subject.ID, nil) &&
			eqFold(o.Kind, e.Kind) && eqFold(o.Severity, e.Severity) &&
			after(&e.OccurredAt, o.Since) &&
			(o.Until == nil || !e.OccurredAt.After(*o.Until))
	})
}

func (b *Backend) Event(ctx context.Context, ref string) (api.Event, error) {
	return find(b, "event", b.state.Events, ref, func(e api.Event) []string {
		return []string{e.ID}
	})
}

// fakeMetricRules mirrors the plane's frozen catalogue of metric-backed rules a
// threshold may name. A copy, and knowingly one: the fake exists to answer
// offline, and there is no plane to ask. `suggested_value` is what a number
// would be if nobody had said otherwise, never a default that fires — no rule
// evaluates until a threshold row declares one.
var fakeMetricRules = []api.MetricRule{
	{Name: "deployment.ttft_high", Metric: "inference.ttft", Unit: "seconds", Comparison: "above", SubjectType: "deployment", Severity: "warning", Measures: "time to first token (p95)", SuggestedValue: 2.0, Lookback: "5m", ForSeconds: 900},
	{Name: "deployment.ttft_p90_high", Metric: "inference.ttft_p90", Unit: "seconds", Comparison: "above", SubjectType: "deployment", Severity: "warning", Measures: "time to first token (p90)", SuggestedValue: 1.5, Lookback: "5m", ForSeconds: 900},
	{Name: "deployment.latency_high", Metric: "inference.latency", Unit: "seconds", Comparison: "above", SubjectType: "deployment", Severity: "error", Measures: "end-to-end latency (p95)", SuggestedValue: 30.0, Lookback: "5m", ForSeconds: 900},
	{Name: "deployment.queue_time_high", Metric: "inference.queue_time", Unit: "seconds", Comparison: "above", SubjectType: "deployment", Severity: "warning", Measures: "time waiting in queue (p95)", SuggestedValue: 1.0, Lookback: "5m", ForSeconds: 900},
	{Name: "deployment.output_speed_below_floor", Metric: "inference.output_tokens_per_stream", Unit: "per_second", Comparison: "below", SubjectType: "deployment", Severity: "warning", Measures: "output speed for one stream", SuggestedValue: 30.0, Lookback: "5m", ForSeconds: 900},
	{Name: "deployment.prefix_cache_hit_rate_low", Metric: "inference.prefix_cache_hit_rate", Unit: "percent", Comparison: "below", SubjectType: "deployment", Severity: "info", Measures: "prefix cache hit rate", SuggestedValue: 20.0, Lookback: "30m", ForSeconds: 3600},
	{Name: "deployment.kv_cache_saturated", Metric: "inference.kv_cache_utilization", Unit: "percent", Comparison: "above", SubjectType: "deployment", Severity: "warning", Measures: "KV cache utilization", SuggestedValue: 95.0, Lookback: "5m", ForSeconds: 900},
	{Name: "deployment.preemptions_high", Metric: "inference.preemptions", Unit: "per_second", Comparison: "above", SubjectType: "deployment", Severity: "warning", Measures: "preemptions per second", SuggestedValue: 0.5, Lookback: "5m", ForSeconds: 900},
	{Name: "node.gpu_temperature_high", Metric: "gpu.temperature", Unit: "celsius", Comparison: "above", SubjectType: "node", Severity: "warning", Measures: "hottest GPU", SuggestedValue: 87.0, Lookback: "5m", ForSeconds: 600},
	{Name: "node.ecc_uncorrectable", Metric: "gpu.ecc_uncorrectable", Unit: "count", Comparison: "above", SubjectType: "node", Severity: "error", Measures: "uncorrectable ECC errors", SuggestedValue: 0.0, Lookback: "5m", ForSeconds: 0},
}

// AlertRules is the catalogue a threshold may name, served rather than compiled
// into a command, so a metric name never becomes a string somebody types.
func (b *Backend) AlertRules(ctx context.Context) ([]api.MetricRule, error) {
	if err := b.injected(); err != nil {
		return nil, err
	}
	return fakeMetricRules, nil
}

func (b *Backend) metricRule(name string) *api.MetricRule {
	for i := range fakeMetricRules {
		if fakeMetricRules[i].Name == name {
			return &fakeMetricRules[i]
		}
	}
	return nil
}

func (b *Backend) AlertThresholds(ctx context.Context, o api.ListOpts) (api.Page[api.AlertThreshold], error) {
	return collect(b, b.state.AlertThresholds, o, func(t api.AlertThreshold) bool {
		return eqFold(o.Rule, t.Rule) && eqFold(o.Kind, t.Subject.Type) &&
			refEqPtr(o.Subject, t.Subject.ID, nil) &&
			boolEq(o.Enabled, t.Enabled) && boolEq(o.FleetDefault, t.FleetDefault())
	})
}

func (b *Backend) AlertThreshold(ctx context.Context, ref string) (api.AlertThreshold, error) {
	return find(b, "alert-threshold", b.state.AlertThresholds, ref, func(t api.AlertThreshold) []string {
		return []string{t.ID}
	})
}

// CreateAlertThreshold refuses what the plane refuses: an unknown rule, and a
// comparison it will not take because which way a metric is wrong is the rule's
// judgement and never declared. `metric` and `comparison` are copied off the
// rule rather than read off the request for the same reason.
func (b *Backend) CreateAlertThreshold(ctx context.Context, req api.AlertThresholdCreate) (api.AlertThreshold, error) {
	if err := b.injected(); err != nil {
		return api.AlertThreshold{}, err
	}
	rule := b.metricRule(req.Rule)
	if rule == nil {
		return api.AlertThreshold{}, errs.New(errs.CodeUsage, "unknown metric rule %q", req.Rule).
			WithFix("List the catalogue", "hgr alert-threshold rules")
	}
	subjectType := orElse(req.SubjectType, rule.SubjectType)
	if !strings.EqualFold(subjectType, rule.SubjectType) {
		return api.AlertThreshold{}, errs.New(errs.CodeUsage, "%s watches a %s, not a %s", rule.Name, rule.SubjectType, subjectType)
	}
	now := time.Now().UTC()
	threshold := api.AlertThreshold{
		ID:         b.newUID("alt_"),
		Rule:       rule.Name,
		Metric:     rule.Metric,
		Comparison: rule.Comparison,
		Subject:    api.AlertSubject{Type: rule.SubjectType, ID: optional(req.SubjectID)},
		Dimensions: req.Dimensions,
		Value:      req.Value,
		Objective:  req.Objective,
		Lookback:   req.Lookback,
		ForSeconds: req.ForSeconds,
		Severity:   req.Severity,
		Enabled:    req.Enabled == nil || *req.Enabled,
		CreatedAt:  now,
	}
	if req.Reason != "" {
		reason := req.Reason
		threshold.Reason = &reason
	}
	b.state.AlertThresholds = append(b.state.AlertThresholds, threshold)
	return threshold, b.persist()
}

func (b *Backend) UpdateAlertThreshold(ctx context.Context, ref string, req api.AlertThresholdUpdate) (api.AlertThreshold, error) {
	threshold, err := b.AlertThreshold(ctx, ref)
	if err != nil {
		return threshold, err
	}
	if req.Value != nil {
		threshold.Value = *req.Value
	}
	if req.Dimensions != nil {
		threshold.Dimensions = req.Dimensions
	}
	if req.Objective != nil {
		threshold.Objective = req.Objective
	}
	if req.Lookback != nil {
		threshold.Lookback = req.Lookback
	}
	if req.ForSeconds != nil {
		threshold.ForSeconds = req.ForSeconds
	}
	if req.Severity != nil {
		threshold.Severity = req.Severity
	}
	if req.Enabled != nil {
		threshold.Enabled = *req.Enabled
	}
	if req.Reason != nil {
		threshold.Reason = req.Reason
	}
	now := time.Now().UTC()
	threshold.UpdatedAt = &now
	for i, existing := range b.state.AlertThresholds {
		if existing.ID == threshold.ID {
			b.state.AlertThresholds[i] = threshold
		}
	}
	return threshold, b.persist()
}

func (b *Backend) DeleteAlertThreshold(ctx context.Context, ref string) error {
	threshold, err := b.AlertThreshold(ctx, ref)
	if err != nil {
		return err
	}
	kept := make([]api.AlertThreshold, 0, len(b.state.AlertThresholds))
	for _, existing := range b.state.AlertThresholds {
		if existing.ID != threshold.ID {
			kept = append(kept, existing)
		}
	}
	b.state.AlertThresholds = kept
	return b.persist()
}

// --- reference resolution --------------------------------------------------

// clusterRef turns a cluster name into its id where it can, so that a filter
// naming a cluster matches rows that carry only the id.
func (b *Backend) clusterRef(ref string) string {
	if ref == "" {
		return ""
	}
	for _, c := range b.state.Clusters {
		if c.ID == ref || strings.EqualFold(c.Name, ref) {
			return c.ID
		}
	}
	return ref
}

func (b *Backend) nodeRef(ref string) string {
	if ref == "" {
		return ""
	}
	for _, n := range b.state.Nodes {
		if n.ID == ref || strings.EqualFold(n.Name, ref) {
			return n.ID
		}
	}
	return ref
}

// devicePool answers `?pool=` for devices, which the plane resolves through
// the device's node.
func (b *Backend) devicePool(d api.Device, pool string) bool {
	if pool == "" {
		return true
	}
	for _, n := range b.state.Nodes {
		if n.ID == d.Node {
			return refEqPtr(pool, n.Pool, n.PoolName)
		}
	}
	return false
}

func deviceInDomain(d api.Device, domain string) bool {
	if domain == "" {
		return true
	}
	if d.Topology == nil {
		return false
	}
	return strings.EqualFold(d.Topology.ID, domain) || strings.EqualFold(d.Topology.Identifier, domain)
}

func allocationHolds(a api.Allocation, device string) bool {
	if device == "" {
		return true
	}
	for _, m := range a.Devices {
		if strings.EqualFold(m.Device, device) || strings.EqualFold(m.DeviceUUID, device) {
			return true
		}
	}
	return false
}

func listHas(values []string, want string) bool {
	if want == "" {
		return true
	}
	for _, v := range values {
		if strings.EqualFold(v, want) {
			return true
		}
	}
	return false
}

func policyBinds(p api.Policy, subjectType, subject string) bool {
	if subjectType == "" && subject == "" {
		return true
	}
	for _, a := range p.Attachments {
		if !eqFold(subjectType, a.SubjectType) {
			continue
		}
		if subject == "" || strings.EqualFold(deref(a.Subject), subject) || strings.EqualFold(deref(a.SubjectName), subject) {
			return true
		}
	}
	return false
}

// normalizeServing fills the containers the wire always carries, so a serving
// block built from flags serialises with the same keys as one read back from
// the plane.
func normalizeServing(sv api.ServingSpec) api.ServingSpec {
	sv.Engine = orElse(sv.Engine, "vllm")
	sv.EngineArgs = orEmpty(sv.EngineArgs)
	sv.ImportedArgs = orEmpty(sv.ImportedArgs)
	sv.Cache.PrefixCacheConfig = orEmpty(sv.Cache.PrefixCacheConfig)
	sv.Overlay.PodSpec = orEmpty(sv.Overlay.PodSpec)
	sv.Overlay.Dynamo = orEmpty(sv.Overlay.Dynamo)
	sv.Behaviour.StreamUsage = orElse(sv.Behaviour.StreamUsage, "final_chunk")
	sv.Behaviour.ContextOverflow = orElse(sv.Behaviour.ContextOverflow, "reject")
	sv.Behaviour.Reasoning.Transport = orElse(sv.Behaviour.Reasoning.Transport, "reasoning_content")
	if sv.Parallelism.Tensor == 0 {
		sv.Parallelism.Tensor = 1
	}
	if sv.Parallelism.Pipeline == 0 {
		sv.Parallelism.Pipeline = 1
	}
	if sv.Parallelism.Expert == 0 {
		sv.Parallelism.Expert = 1
	}
	if sv.Shape.DeviceCount == 0 {
		sv.Shape.DeviceCount = 1
	}
	sv.Topology.ServingTopology = orElse(sv.Topology.ServingTopology, "aggregated")
	if sv.Topology.Roles == nil {
		sv.Topology.Roles = []api.ServingRole{}
	}
	sv.Topology.GroupDevices = sv.Shape.DeviceCount
	if sv.Capabilities.StructuredOutputModes == nil {
		sv.Capabilities.StructuredOutputModes = []string{"text"}
	}
	if sv.Capabilities.ToolChoiceModes == nil {
		sv.Capabilities.ToolChoiceModes = []string{}
	}
	if sv.Capabilities.InputModalities == nil {
		sv.Capabilities.InputModalities = []string{"text"}
	}
	sv.Capabilities.PromptCacheMode = orElse(sv.Capabilities.PromptCacheMode, "off")
	sv.ResolvedEngineArgs = resolvedEngineArgs(sv)
	return sv
}

// servingOf turns the block a caller sent into the block a read returns. The
// artifact is resolved to its id here, and the derivations are the plane's:
// nothing a client sent decides them.
func servingOf(artifactID string, req api.ServingRequest) api.ServingSpec {
	behaviour := api.DefaultServingBehaviour()
	if req.Behaviour != nil {
		behaviour = *req.Behaviour
	}
	var capabilities api.ServingCapabilities
	if req.Capabilities != nil {
		capabilities = *req.Capabilities
	}
	return api.ServingSpec{
		ArtifactID: artifactID,
		Engine:     req.Engine, EngineRuntimeVersion: req.EngineRuntimeVersion,
		EngineRuntimeImage: req.EngineRuntimeImage, FrontendImage: req.FrontendImage,
		EngineArgs: req.EngineArgs, ImportedArgs: req.ImportedArgs,
		Quantization: req.Quantization, Dtype: req.Dtype,
		Parallelism:   req.Parallelism,
		ContextLength: req.ContextLength, MaxBatchTokens: req.MaxBatchTokens,
		Cache: req.Cache, Behaviour: behaviour, Capabilities: capabilities,
		Shape: req.Shape, Resources: req.Resources, Overlay: req.Overlay,
	}
}

// resolvedEngineArgs is the declared args with the flags the shape dictates laid
// over them, keyed the way the operator keys them — without the leading dashes.
// The plane derives more than this; what matters to a client is that a declared
// arg is never overwritten by a derived one.
func resolvedEngineArgs(sv api.ServingSpec) map[string]any {
	out := map[string]any{"tensor-parallel-size": sv.Parallelism.Tensor}
	if sv.ContextLength > 0 {
		out["max-model-len"] = sv.ContextLength
	}
	for k, v := range sv.EngineArgs {
		out[strings.TrimLeft(k, "-")] = v
	}
	return out
}

// servingDigest is the identity of a serving configuration: the same
// configuration over the same artifact digests the same, which is the whole
// mechanism that replaced matching one object against another by hand.
//
// Observed and asserted fields are excluded, exactly as the plane excludes them:
// the digest answers "would this produce the same running process", so only what
// reaches an engine is in it.
func servingDigest(sv api.ServingSpec) string {
	sv.ImportedArgs = nil
	sv.Capabilities = api.ServingCapabilities{}
	sv.ResolvedEngineArgs = nil
	body, err := json.Marshal(sv)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(body)
	return "svg_" + hex.EncodeToString(sum[:])
}

func orEmpty(m map[string]any) map[string]any {
	if m == nil {
		return map[string]any{}
	}
	return m
}
