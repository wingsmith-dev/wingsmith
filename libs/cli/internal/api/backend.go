package api

import "context"

// Backend is everything the CLI can ask of the control plane. The HTTP client
// and the fake both implement it, so no command talks to a transport and a
// command tested against the fake is a command tested for real shape.
//
// Every list returns a Page: the envelope is part of the contract, and a
// command that wants the whole collection sets ListOpts.All rather than
// assuming the first page was all of it.
type Backend interface {
	Identity(ctx context.Context) (Identity, error)

	Clusters(ctx context.Context, opts ListOpts) (Page[Cluster], error)
	Cluster(ctx context.Context, ref string) (Cluster, error)
	Nodes(ctx context.Context, opts ListOpts) (Page[Node], error)
	Node(ctx context.Context, ref string) (Node, error)
	Devices(ctx context.Context, opts ListOpts) (Page[Device], error)
	Device(ctx context.Context, ref string) (Device, error)
	DeviceClasses(ctx context.Context, opts ListOpts) (Page[DeviceClass], error)
	DeviceClass(ctx context.Context, ref string) (DeviceClass, error)
	TopologyDomains(ctx context.Context, opts ListOpts) (Page[TopologyDomain], error)
	TopologyDomain(ctx context.Context, ref string) (TopologyDomain, error)
	Pools(ctx context.Context, opts ListOpts) (Page[Pool], error)
	Pool(ctx context.Context, ref string) (Pool, error)

	Artifacts(ctx context.Context, opts ListOpts) (Page[Artifact], error)
	Artifact(ctx context.Context, ref string) (Artifact, error)
	Workloads(ctx context.Context, opts ListOpts) (Page[Workload], error)
	Workload(ctx context.Context, ref string) (Workload, error)
	CreateWorkload(ctx context.Context, req WorkloadCreateRequest) (Workload, error)
	UpdateWorkload(ctx context.Context, ref string, req WorkloadUpdateRequest) (Workload, error)
	DeleteWorkload(ctx context.Context, ref string) error
	Deployments(ctx context.Context, opts ListOpts) (Page[Deployment], error)
	Deployment(ctx context.Context, ref string) (Deployment, error)
	UpdateDeployment(ctx context.Context, ref string, req DeploymentUpdateRequest) (Deployment, error)
	DiscoveredWorkloads(ctx context.Context, opts ListOpts) (Page[DiscoveredWorkload], error)
	DiscoveredWorkload(ctx context.Context, ref string) (DiscoveredWorkload, error)
	DecideWorkload(ctx context.Context, ref, decision string, req DecisionRequest) (DiscoveredWorkload, error)
	RemoveWorkload(ctx context.Context, ref string) error
	RestoreWorkload(ctx context.Context, ref string) (DiscoveredWorkload, error)
	PromoteWorkload(ctx context.Context, ref string, req PromoteRequest) (Workload, error)
	Endpoints(ctx context.Context, opts ListOpts) (Page[Endpoint], error)
	Endpoint(ctx context.Context, ref string) (Endpoint, error)

	Tenants(ctx context.Context, opts ListOpts) (Page[Tenant], error)
	Tenant(ctx context.Context, ref string) (Tenant, error)
	Projects(ctx context.Context, opts ListOpts) (Page[Project], error)
	Project(ctx context.Context, ref string) (Project, error)
	Quotas(ctx context.Context, opts ListOpts) (Page[Quota], error)
	Quota(ctx context.Context, ref string) (Quota, error)
	PriorityClasses(ctx context.Context, opts ListOpts) (Page[PriorityClass], error)
	PriorityClass(ctx context.Context, ref string) (PriorityClass, error)
	Policies(ctx context.Context, opts ListOpts) (Page[Policy], error)
	Policy(ctx context.Context, ref string) (Policy, error)
	ServingQuotas(ctx context.Context, opts ListOpts) (Page[ServingQuota], error)
	ServingQuota(ctx context.Context, ref string) (ServingQuota, error)
	CreateServingQuota(ctx context.Context, req ServingQuotaCreate) (ServingQuota, error)
	UpdateServingQuota(ctx context.Context, ref string, req ServingQuotaUpdate) (ServingQuota, error)
	DeleteServingQuota(ctx context.Context, ref string) error
	ResolvedServingQuotas(ctx context.Context, endpoint, workload string, replicas, index int) (ResolvedServingQuotaTable, error)

	Allocations(ctx context.Context, opts ListOpts) (Page[Allocation], error)
	Allocation(ctx context.Context, ref string) (Allocation, error)
	Operations(ctx context.Context, opts ListOpts) (Page[Operation], error)
	Operation(ctx context.Context, ref string) (Operation, error)
	CreateOperation(ctx context.Context, req OperationRequest, idempotencyKey string) (Operation, error)
	CancelOperation(ctx context.Context, ref string) (Operation, error)

	Benchmarks(ctx context.Context, opts ListOpts) (Page[BenchmarkRun], error)
	Benchmark(ctx context.Context, ref string) (BenchmarkRun, error)
	BenchmarkVerdict(ctx context.Context, ref, criteria string) (BenchmarkVerdict, error)
	BenchmarkHarnesses(ctx context.Context) ([]BenchmarkHarness, error)
	CreateBenchmark(ctx context.Context, req BenchmarkRunCreate) (BenchmarkRun, error)
	BenchmarkRequest(ctx context.Context, ref string) (BenchmarkRequest, error)
	CreateBenchmarkRequest(ctx context.Context, req BenchmarkRequestCreate) (BenchmarkRequest, error)
	BenchmarkRequestVerdict(ctx context.Context, ref, criteria string) (BenchmarkVerdict, error)
	BenchmarkRequestBands(ctx context.Context, ref string) ([]BenchmarkBand, error)
	Components(ctx context.Context, opts ListOpts) (Page[Component], error)
	Component(ctx context.Context, ref string) (Component, error)
	Events(ctx context.Context, opts ListOpts) (Page[Event], error)
	Event(ctx context.Context, ref string) (Event, error)
	AlertThresholds(ctx context.Context, opts ListOpts) (Page[AlertThreshold], error)
	AlertThreshold(ctx context.Context, ref string) (AlertThreshold, error)
	CreateAlertThreshold(ctx context.Context, req AlertThresholdCreate) (AlertThreshold, error)
	UpdateAlertThreshold(ctx context.Context, ref string, req AlertThresholdUpdate) (AlertThreshold, error)
	DeleteAlertThreshold(ctx context.Context, ref string) error
	AlertRules(ctx context.Context) ([]MetricRule, error)

	// Simulated reports whether the results are synthetic.
	Simulated() bool
}
