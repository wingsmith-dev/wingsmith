package fake

import (
	"context"
	"strings"
	"time"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
)

// The filters here are the ones the plane's routes declare, and only those. A
// filter the server does not implement is a filter the fake must not honour,
// or the fake becomes a place where wrong client behaviour looks right.

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

func (b *Backend) Recipes(ctx context.Context, o api.ListOpts) (api.Page[api.Recipe], error) {
	return collect(b, b.state.Recipes, o, func(r api.Recipe) bool {
		return refEq(o.Artifact, r.ArtifactID) && eqFold(o.Engine, r.Engine) &&
			eqFold(o.Class, r.Shape.DeviceClass) &&
			contains(o.Search, r.Name, r.ID, r.Engine) &&
			labelsMatch(r.Labels, o.Labels)
	})
}

func (b *Backend) Recipe(ctx context.Context, ref string) (api.Recipe, error) {
	return find(b, "recipe", b.state.Recipes, ref, func(r api.Recipe) []string {
		return []string{r.Name, r.ID}
	})
}

func (b *Backend) Deployments(ctx context.Context, o api.ListOpts) (api.Page[api.Deployment], error) {
	cluster := b.clusterRef(o.Cluster)
	return collect(b, b.state.Deployments, o, func(d api.Deployment) bool {
		return refEq(cluster, d.ClusterID) && refEq(o.Recipe, d.RecipeID) &&
			refEq(o.Project, d.Workload.ProjectID) &&
			eqFold(o.Phase, d.Workload.Phase) && eqFold(o.Type, d.Workload.Type) &&
			labelsMatch(d.Labels, o.Labels)
	})
}

func (b *Backend) Deployment(ctx context.Context, ref string) (api.Deployment, error) {
	return find(b, "deployment", b.state.Deployments, ref, func(d api.Deployment) []string {
		return []string{d.Name, d.ID}
	})
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
	if decision == "ignore" {
		return "ignored"
	}
	return "tracked"
}

// PromoteWorkload mirrors the plane's gate, refusals included. A fake that promoted anything it
// was asked to would let a command that cannot work in production pass its tests.
func (b *Backend) PromoteWorkload(ctx context.Context, ref string, req api.PromoteRequest) (api.Deployment, error) {
	found, err := b.DiscoveredWorkload(ctx, ref)
	if err != nil {
		return api.Deployment{}, err
	}
	if found.DeploymentID != nil {
		return api.Deployment{}, errs.New(errs.CodeConflict, "%q was already promoted", found.Name)
	}
	if found.SpecFingerprint == nil || *found.SpecFingerprint == "" {
		return api.Deployment{}, errs.New(errs.CodeInvalidState,
			"this cluster has not reported what %q is running, so there is nothing to pin the adoption to", found.Name)
	}
	if req.Fingerprint != *found.SpecFingerprint {
		return api.Deployment{}, errs.New(errs.CodeConflict,
			"%q changed since it was read; re-read it and promote again", found.Name)
	}
	recipe, err := b.Recipe(ctx, req.Recipe)
	if err != nil {
		return api.Deployment{}, err
	}
	project, err := b.Project(ctx, req.Project)
	if err != nil {
		return api.Deployment{}, err
	}

	now := time.Now().UTC()
	name := req.Name
	if name == "" {
		name = found.Name
	}
	deployment := api.Deployment{
		ID: b.newUID("dep_"), Name: name, ClusterID: found.ClusterID, RecipeID: recipe.ID,
		MinReplicas:     found.ReplicasDesired,
		MaxReplicas:     found.ReplicasDesired,
		DesiredReplicas: found.ReplicasDesired,
		Generation:      1, Enabled: true,
		AdapterArtifactIDs: []string{},
		Overrides:          map[string]any{},
		Labels:             found.Labels,
		CreatedAt:          now,
		Revision:           revision(now),
		AdoptedWorkloadID:  &found.ID,
		AdoptedFingerprint: found.SpecFingerprint,
		Workload: api.DeploymentWorkload{
			Type: "service", Phase: "running", ProjectID: project.ID,
			PriorityClass: orElse(req.PriorityClass, "production"),
			GangSize:      1, QueuedAt: &now, StartedAt: &now,
		},
		Replicas: api.DeploymentReplicaRollup{
			Total: found.ReplicasDesired, Ready: found.ReplicasReady, Running: found.ReplicasReady,
			AtCurrentGeneration: found.ReplicasReady,
		},
	}
	b.state.Deployments = append(b.state.Deployments, deployment)
	for i := range b.state.Discovered {
		if b.state.Discovered[i].ID == found.ID {
			b.state.Discovered[i].Status = "promoted"
			b.state.Discovered[i].DeploymentID = &deployment.ID
			b.state.Discovered[i].ProjectID = &project.ID
		}
	}
	if err := b.persist(); err != nil {
		return api.Deployment{}, err
	}
	return deployment, nil
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
			contains(o.Search, e.Name, e.ID, deref(e.DisplayName)) &&
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
	Image:       "nvcr.io/nvidia/ai-dynamo/aiperf:latest",
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
		return refEqPtr(cluster, &run.ClusterID, nil) &&
			refEq(o.Deployment, run.DeploymentID) && eqFold(o.Harness, run.Harness) &&
			eqFold(o.Phase, run.Phase) && eqFold(o.Actor, run.RequestedBy) &&
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
func (b *Backend) CreateBenchmark(ctx context.Context, req api.BenchmarkRequest) (api.BenchmarkRun, error) {
	if err := b.injected(); err != nil {
		return api.BenchmarkRun{}, err
	}
	if strings.TrimSpace(req.Deployment) == "" {
		return api.BenchmarkRun{}, errs.New(errs.CodeInputRequired, "a benchmark needs a deployment")
	}
	var harness *api.BenchmarkHarness
	for i := range fakeHarnesses {
		if eqFold(req.Harness, fakeHarnesses[i].Name) || req.Harness == "" {
			harness = &fakeHarnesses[i]
			break
		}
	}
	if harness == nil {
		return api.BenchmarkRun{}, errs.New(errs.CodeUsage, "unknown benchmark harness %q", req.Harness)
	}
	known := map[string]bool{}
	for _, parameter := range harness.Parameters {
		known[parameter.Name] = true
	}
	for name := range req.Parameters {
		if !known[name] {
			return api.BenchmarkRun{}, errs.New(errs.CodeUsage, "%s does not accept %q", harness.Name, name)
		}
	}
	deployment, err := b.Deployment(ctx, req.Deployment)
	if err != nil {
		return api.BenchmarkRun{}, err
	}
	parameters := map[string]any{}
	for _, parameter := range harness.Parameters {
		parameters[parameter.Name] = parameter.Default
	}
	for name, value := range req.Parameters {
		parameters[name] = value
	}
	run := api.BenchmarkRun{
		ID:           b.newUID("bmk_"),
		DeploymentID: deployment.ID,
		ClusterID:    deployment.ClusterID,
		Harness:      harness.Name,
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

// CreateOperation records an operation against a target the fake can resolve,
// so `--fake` refuses the same nonsense the plane would.
func (b *Backend) CreateOperation(ctx context.Context, req api.OperationRequest, idempotencyKey string) (api.Operation, error) {
	if err := b.injected(); err != nil {
		return api.Operation{}, err
	}
	if strings.TrimSpace(req.Type) == "" || strings.TrimSpace(req.Target) == "" {
		return api.Operation{}, errs.New(errs.CodeInputRequired, "an operation needs a type and a target")
	}
	if idempotencyKey != "" {
		for _, op := range b.state.Operations {
			if op.Parameters["idempotency_key"] == idempotencyKey {
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
	if idempotencyKey != "" {
		params["idempotency_key"] = idempotencyKey
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
