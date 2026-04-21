// Package api holds the wire types and the HTTP client for the Hangar
// control plane. The CLI speaks to the plane's API and to nothing else: it
// never holds a kubeconfig and never dials a cluster or the hub.
package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
)

// Client is the live control plane transport.
type Client struct {
	Server  string
	Token   string
	Scope   string
	HTTP    *http.Client
	Version string
}

// NewClient builds a client against the given base URL.
func NewClient(server, token, project, version string, timeout time.Duration) (*Client, error) {
	if server == "" {
		return nil, errs.New(errs.CodeUsage, "no control plane URL configured").
			WithFix("Set one", "hgr config set context.server https://plane.hangar.dev").
			WithFix("Or run offline", "hgr --fake cluster ls")
	}
	if _, err := url.Parse(server); err != nil {
		return nil, errs.Wrap(err, errs.CodeUsage, "invalid control plane URL %q", server)
	}
	return &Client{
		Server:  strings.TrimRight(server, "/"),
		Token:   token,
		Scope:   project,
		Version: version,
		HTTP:    &http.Client{Timeout: timeout},
	}, nil
}

func (c *Client) Simulated() bool { return false }

// Identity reports the configured context and whether the plane answers.
// There is no `/v1/whoami`: auth is one static token and the plane resolves
// every caller to the same principal, so inventing a user here would be
// fiction. `/health` is the only thing that can be truthfully asked.
func (c *Client) Identity(ctx context.Context) (Identity, error) {
	id := Identity{Server: c.Server, Project: c.Scope, HasToken: c.Token != ""}
	var health struct {
		Status bool `json:"status"`
	}
	if err := c.get(ctx, "/health", nil, &health); err != nil {
		return id, err
	}
	id.Reachable = health.Status
	return id, nil
}

// --- query construction ----------------------------------------------------

// query builds a query string from the dimensions one endpoint declares.
// Nothing is sent that the endpoint does not accept: a filter the server does
// not know is a filter silently ignored, and a listing that quietly ignores
// its filter is worse than one that refuses it.
type query struct {
	v url.Values
}

func newQuery(opts ListOpts) *query {
	q := &query{v: url.Values{}}
	if opts.Limit > 0 {
		q.v.Set("limit", strconv.Itoa(opts.Limit))
	}
	if opts.Cursor != "" {
		q.v.Set("cursor", opts.Cursor)
	}
	return q
}

func (q *query) str(name, value string) *query {
	if value != "" {
		q.v.Set(name, value)
	}
	return q
}

func (q *query) boolean(name string, value *bool) *query {
	if value != nil {
		q.v.Set(name, strconv.FormatBool(*value))
	}
	return q
}

func (q *query) at(name string, value *time.Time) *query {
	if value != nil {
		q.v.Set(name, value.UTC().Format(time.RFC3339))
	}
	return q
}

// labels emits one `label=k=v` per selector entry, which is the repeatable
// form the plane parses.
func (q *query) labels(selector map[string]string) *query {
	for k, v := range selector {
		q.v.Add("label", k+"="+v)
	}
	return q
}

func (q *query) values() url.Values { return q.v }

// --- generic list and get --------------------------------------------------

// listPage fetches exactly one page.
func listPage[T any](ctx context.Context, c *Client, path string, q url.Values) (Page[T], error) {
	var out Page[T]
	err := c.get(ctx, path, q, &out)
	return out, err
}

// list fetches one page, or every page when ListOpts.All is set. Following
// the cursor is the client's job: the server has no "give me everything" mode
// and adding one would be a request with no bound on its own cost.
func list[T any](ctx context.Context, c *Client, path string, q url.Values, opts ListOpts) (Page[T], error) {
	page, err := listPage[T](ctx, c, path, q)
	if err != nil || !opts.All {
		return page, err
	}
	for page.NextCursor != "" {
		next := cloneValues(q)
		next.Set("cursor", page.NextCursor)
		more, err := listPage[T](ctx, c, path, next)
		if err != nil {
			return page, err
		}
		page.Items = append(page.Items, more.Items...)
		page.NextCursor = more.NextCursor
		if more.Count == 0 {
			break
		}
	}
	page.Count = len(page.Items)
	return page, nil
}

func cloneValues(v url.Values) url.Values {
	out := make(url.Values, len(v))
	for k, vals := range v {
		out[k] = append([]string(nil), vals...)
	}
	return out
}

func one[T any](ctx context.Context, c *Client, path, ref string, q url.Values) (T, error) {
	var out T
	err := c.get(ctx, path+"/"+url.PathEscape(ref), q, &out)
	return out, err
}

// --- supply ----------------------------------------------------------------

func (c *Client) Clusters(ctx context.Context, o ListOpts) (Page[Cluster], error) {
	q := newQuery(o).str("region", o.Region).str("phase", o.Phase).str("search", o.Search).labels(o.Labels)
	return list[Cluster](ctx, c, "/v1/clusters", q.values(), o)
}

func (c *Client) Cluster(ctx context.Context, ref string) (Cluster, error) {
	return one[Cluster](ctx, c, "/v1/clusters", ref, nil)
}

func (c *Client) Nodes(ctx context.Context, o ListOpts) (Page[Node], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("pool", o.Pool).str("condition", o.Condition).
		boolean("ready", o.Ready).boolean("cordoned", o.Cordoned).str("search", o.Search).labels(o.Labels)
	return list[Node](ctx, c, "/v1/nodes", q.values(), o)
}

func (c *Client) Node(ctx context.Context, ref string) (Node, error) {
	return one[Node](ctx, c, "/v1/nodes", ref, nil)
}

func (c *Client) Devices(ctx context.Context, o ListOpts) (Page[Device], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("node", o.Node).str("pool", o.Pool).
		str("class", o.Class).str("health", o.Health).boolean("allocatable", o.Allocatable).
		str("topology_domain", o.TopologyDomain)
	return list[Device](ctx, c, "/v1/devices", q.values(), o)
}

func (c *Client) Device(ctx context.Context, ref string) (Device, error) {
	return one[Device](ctx, c, "/v1/devices", ref, nil)
}

func (c *Client) DeviceClasses(ctx context.Context, o ListOpts) (Page[DeviceClass], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("architecture", o.Architecture).
		boolean("present", o.Present)
	return list[DeviceClass](ctx, c, "/v1/device-classes", q.values(), o)
}

func (c *Client) DeviceClass(ctx context.Context, ref string) (DeviceClass, error) {
	return one[DeviceClass](ctx, c, "/v1/device-classes", ref, nil)
}

func (c *Client) TopologyDomains(ctx context.Context, o ListOpts) (Page[TopologyDomain], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("node", o.Node).str("kind", o.Kind).
		boolean("spans_nodes", o.SpansNodes)
	return list[TopologyDomain](ctx, c, "/v1/topology-domains", q.values(), o)
}

func (c *Client) TopologyDomain(ctx context.Context, ref string) (TopologyDomain, error) {
	return one[TopologyDomain](ctx, c, "/v1/topology-domains", ref, nil)
}

func (c *Client) Pools(ctx context.Context, o ListOpts) (Page[Pool], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("class", o.Class).boolean("enabled", o.Enabled).
		str("search", o.Search).labels(o.Labels)
	return list[Pool](ctx, c, "/v1/pools", q.values(), o)
}

func (c *Client) Pool(ctx context.Context, ref string) (Pool, error) {
	return one[Pool](ctx, c, "/v1/pools", ref, nil)
}

// --- demand ----------------------------------------------------------------

func (c *Client) Artifacts(ctx context.Context, o ListOpts) (Page[Artifact], error) {
	q := newQuery(o).str("kind", o.Kind).str("format", o.Format).str("project", o.Project).
		str("search", o.Search).labels(o.Labels)
	return list[Artifact](ctx, c, "/v1/artifacts", q.values(), o)
}

func (c *Client) Artifact(ctx context.Context, ref string) (Artifact, error) {
	return one[Artifact](ctx, c, "/v1/artifacts", ref, nil)
}

func (c *Client) Recipes(ctx context.Context, o ListOpts) (Page[Recipe], error) {
	q := newQuery(o).str("artifact", o.Artifact).str("engine", o.Engine).str("class", o.Class).
		str("search", o.Search).labels(o.Labels)
	return list[Recipe](ctx, c, "/v1/recipes", q.values(), o)
}

func (c *Client) Recipe(ctx context.Context, ref string) (Recipe, error) {
	return one[Recipe](ctx, c, "/v1/recipes", ref, nil)
}

func (c *Client) Workloads(ctx context.Context, o ListOpts) (Page[Workload], error) {
	q := newQuery(o).str("project", o.Project).str("kind", o.Kind).str("phase", o.Phase).
		str("recipe", o.Recipe).str("cluster", o.Cluster).labels(o.Labels)
	return list[Workload](ctx, c, "/v1/workloads", q.values(), o)
}

// Workload resolves by id or by name. A name is unique within a project and not
// globally, so a bare name outside one project is ambiguous and the plane says
// so rather than picking.
func (c *Client) Workload(ctx context.Context, ref string) (Workload, error) {
	return one[Workload](ctx, c, "/v1/workloads", ref, nil)
}

func (c *Client) CreateWorkload(ctx context.Context, req WorkloadCreateRequest) (Workload, error) {
	var out Workload
	err := c.send(ctx, http.MethodPost, "/v1/workloads", nil, req, nil, &out)
	return out, err
}

func (c *Client) UpdateWorkload(ctx context.Context, ref string, req WorkloadUpdateRequest) (Workload, error) {
	var out Workload
	err := c.send(ctx, http.MethodPatch, "/v1/workloads/"+url.PathEscape(ref), nil, req, nil, &out)
	return out, err
}

func (c *Client) DeleteWorkload(ctx context.Context, ref string) error {
	return c.send(ctx, http.MethodDelete, "/v1/workloads/"+url.PathEscape(ref), nil, nil, nil, nil)
}

// Deployments lists the per-cluster shares. There is no POST and no DELETE here:
// placement creates a deployment when it expands a workload, and taking a
// workload out of one cluster is `workload.drain` or a scale change.
func (c *Client) Deployments(ctx context.Context, o ListOpts) (Page[Deployment], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("workload", o.Workload).
		str("project", o.Project).str("phase", o.Phase)
	return list[Deployment](ctx, c, "/v1/deployments", q.values(), o)
}

// Deployment resolves by id, or by name within a cluster: a deployment name
// is unique per cluster and not globally — two clusters holding shares of one
// workload share a name — so `--cluster` is how a name is made unambiguous.
func (c *Client) Deployment(ctx context.Context, ref string) (Deployment, error) {
	return one[Deployment](ctx, c, "/v1/deployments", ref, nil)
}

func (c *Client) UpdateDeployment(ctx context.Context, ref string, req DeploymentUpdateRequest) (Deployment, error) {
	var out Deployment
	err := c.send(ctx, http.MethodPatch, "/v1/deployments/"+url.PathEscape(ref), nil, req, nil, &out)
	return out, err
}

func (c *Client) DiscoveredWorkloads(ctx context.Context, o ListOpts) (Page[DiscoveredWorkload], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("namespace", o.Namespace).
		str("status", o.Status).str("search", o.Search)
	return list[DiscoveredWorkload](ctx, c, "/v1/discovered", q.values(), o)
}

func (c *Client) DiscoveredWorkload(ctx context.Context, ref string) (DiscoveredWorkload, error) {
	return one[DiscoveredWorkload](ctx, c, "/v1/discovered", ref, nil)
}

// DecideWorkload records track or ignore. It is a POST on a subpath rather than
// a PATCH on the workload because the object it writes is not the workload: the
// sighting is observed and has exactly one writer, which is ingestion.
func (c *Client) DecideWorkload(ctx context.Context, ref, decision string, req DecisionRequest) (DiscoveredWorkload, error) {
	var out DiscoveredWorkload
	path := "/v1/discovered/" + url.PathEscape(ref) + "/" + decision
	err := c.send(ctx, http.MethodPost, path, nil, req, nil, &out)
	return out, err
}

// RemoveWorkload takes a sighting off the list and keeps it off.
//
// A DELETE that deletes nothing observed: the row belongs to ingestion and would be written back
// on the next observation anyway, because the workload is still running. What it writes is the
// decision, and RestoreWorkload undoes it.
func (c *Client) RemoveWorkload(ctx context.Context, ref string) error {
	path := "/v1/discovered/" + url.PathEscape(ref)
	return c.send(ctx, http.MethodDelete, path, nil, nil, nil, nil)
}

// RestoreWorkload puts a removed sighting back on the list, with no decision on it.
func (c *Client) RestoreWorkload(ctx context.Context, ref string) (DiscoveredWorkload, error) {
	var out DiscoveredWorkload
	path := "/v1/discovered/" + url.PathEscape(ref) + "/restore"
	err := c.send(ctx, http.MethodPost, path, nil, nil, nil, &out)
	return out, err
}

// PromoteWorkload returns the Workload it made, pinned to the one cluster the sighting is running
// in. From here the object is managed through /v1/workloads like any other, and its single
// deployment is the cluster's copy.
func (c *Client) PromoteWorkload(ctx context.Context, ref string, req PromoteRequest) (Workload, error) {
	var out Workload
	path := "/v1/discovered/" + url.PathEscape(ref) + "/promote"
	err := c.send(ctx, http.MethodPost, path, nil, req, nil, &out)
	return out, err
}

func (c *Client) Endpoints(ctx context.Context, o ListOpts) (Page[Endpoint], error) {
	q := newQuery(o).str("project", o.Project).str("artifact", o.Artifact).
		str("search", o.Search).labels(o.Labels)
	return list[Endpoint](ctx, c, "/v1/endpoints", q.values(), o)
}

func (c *Client) Endpoint(ctx context.Context, ref string) (Endpoint, error) {
	return one[Endpoint](ctx, c, "/v1/endpoints", ref, nil)
}

// --- binding ---------------------------------------------------------------

func (c *Client) Tenants(ctx context.Context, o ListOpts) (Page[Tenant], error) {
	q := newQuery(o).boolean("enabled", o.Enabled).str("search", o.Search)
	return list[Tenant](ctx, c, "/v1/tenants", q.values(), o)
}

func (c *Client) Tenant(ctx context.Context, ref string) (Tenant, error) {
	return one[Tenant](ctx, c, "/v1/tenants", ref, nil)
}

func (c *Client) Projects(ctx context.Context, o ListOpts) (Page[Project], error) {
	q := newQuery(o).str("tenant", o.Tenant).boolean("enabled", o.Enabled).str("search", o.Search)
	return list[Project](ctx, c, "/v1/projects", q.values(), o)
}

func (c *Client) Project(ctx context.Context, ref string) (Project, error) {
	return one[Project](ctx, c, "/v1/projects", ref, nil)
}

func (c *Client) Quotas(ctx context.Context, o ListOpts) (Page[Quota], error) {
	q := newQuery(o).str("project", o.Project).str("cluster", o.Cluster).str("class", o.Class).
		boolean("over_quota_enabled", o.OverQuotaEnabled)
	return list[Quota](ctx, c, "/v1/quotas", q.values(), o)
}

func (c *Client) Quota(ctx context.Context, ref string) (Quota, error) {
	return one[Quota](ctx, c, "/v1/quotas", ref, nil)
}

func (c *Client) PriorityClasses(ctx context.Context, o ListOpts) (Page[PriorityClass], error) {
	q := newQuery(o).boolean("preemptible", o.Preemptible).boolean("preempts", o.Preempts).str("search", o.Search)
	return list[PriorityClass](ctx, c, "/v1/priority-classes", q.values(), o)
}

func (c *Client) PriorityClass(ctx context.Context, ref string) (PriorityClass, error) {
	return one[PriorityClass](ctx, c, "/v1/priority-classes", ref, nil)
}

func (c *Client) Policies(ctx context.Context, o ListOpts) (Page[Policy], error) {
	q := newQuery(o).boolean("enabled", o.Enabled).str("region", o.Region).str("class", o.Class).
		str("search", o.Search).str("subject_type", o.Kind).str("subject", o.Subject)
	return list[Policy](ctx, c, "/v1/policies", q.values(), o)
}

func (c *Client) Policy(ctx context.Context, ref string) (Policy, error) {
	return one[Policy](ctx, c, "/v1/policies", ref, nil)
}

// ServingQuotas is the traffic collection, and it is not /v1/quotas with a
// filter. A device quota and a serving quota share a word and nothing else:
// different table, different enforcer, different moment.
func (c *Client) ServingQuotas(ctx context.Context, o ListOpts) (Page[ServingQuota], error) {
	q := newQuery(o).str("endpoint", o.Endpoint).str("project", o.Project).
		boolean("enforced", o.Enforced).str("scope", o.Scope)
	return list[ServingQuota](ctx, c, "/v1/serving-quotas", q.values(), o)
}

func (c *Client) ServingQuota(ctx context.Context, ref string) (ServingQuota, error) {
	return one[ServingQuota](ctx, c, "/v1/serving-quotas", ref, nil)
}

func (c *Client) CreateServingQuota(ctx context.Context, req ServingQuotaCreate) (ServingQuota, error) {
	var out ServingQuota
	err := c.send(ctx, http.MethodPost, "/v1/serving-quotas", nil, req, nil, &out)
	return out, err
}

func (c *Client) UpdateServingQuota(ctx context.Context, ref string, req ServingQuotaUpdate) (ServingQuota, error) {
	var out ServingQuota
	err := c.send(ctx, http.MethodPatch, "/v1/serving-quotas/"+url.PathEscape(ref), nil, req, nil, &out)
	return out, err
}

func (c *Client) DeleteServingQuota(ctx context.Context, ref string) error {
	return c.send(ctx, http.MethodDelete, "/v1/serving-quotas/"+url.PathEscape(ref), nil, nil, nil, nil)
}

// ResolvedServingQuotas asks what a commitment means for a fleet of a stated
// size. The replica count travels from the caller because the plane cannot
// discover it without putting a Kubernetes client on the inference path.
func (c *Client) ResolvedServingQuotas(ctx context.Context, endpoint, workload string, replicas, index int) (ResolvedServingQuotaTable, error) {
	q := url.Values{}
	if endpoint != "" {
		q.Set("endpoint", endpoint)
	}
	if workload != "" {
		q.Set("workload", workload)
	}
	q.Set("replicas", strconv.Itoa(replicas))
	q.Set("replica_index", strconv.Itoa(index))
	var out ResolvedServingQuotaTable
	err := c.get(ctx, "/v1/serving-quotas/resolved", q, &out)
	return out, err
}

// --- machinery -------------------------------------------------------------

func (c *Client) Allocations(ctx context.Context, o ListOpts) (Page[Allocation], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("project", o.Project).str("class", o.Class).
		str("phase", o.Phase).boolean("over_quota", o.OverQuota).boolean("live", o.Live).
		str("replica", o.Replica).str("device", o.Device)
	return list[Allocation](ctx, c, "/v1/allocations", q.values(), o)
}

func (c *Client) Allocation(ctx context.Context, ref string) (Allocation, error) {
	return one[Allocation](ctx, c, "/v1/allocations", ref, nil)
}

func (c *Client) Operations(ctx context.Context, o ListOpts) (Page[Operation], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("type", o.Type).str("phase", o.Phase).
		str("target", o.Target).str("actor", o.Actor).at("since", o.Since)
	return list[Operation](ctx, c, "/v1/operations", q.values(), o)
}

func (c *Client) Operation(ctx context.Context, ref string) (Operation, error) {
	return one[Operation](ctx, c, "/v1/operations", ref, nil)
}

// CreateOperation posts a type and a target. There is no verb endpoint on the
// target's own path, so this is the only way to drain, cordon, reset or warm
// anything.
func (c *Client) CreateOperation(ctx context.Context, req OperationRequest, idempotencyKey string) (Operation, error) {
	var out Operation
	headers := map[string]string{}
	if idempotencyKey != "" {
		headers["Idempotency-Key"] = idempotencyKey
	}
	err := c.send(ctx, http.MethodPost, "/v1/operations", nil, req, headers, &out)
	return out, err
}

// CancelOperation asks for cancellation and returns the operation. DELETE is
// cancellation and not deletion: the row is the answer to who asked for this
// and why, which is the reason the object exists.
func (c *Client) CancelOperation(ctx context.Context, ref string) (Operation, error) {
	var out Operation
	err := c.send(ctx, http.MethodDelete, "/v1/operations/"+url.PathEscape(ref), nil, nil, nil, &out)
	return out, err
}

func (c *Client) Benchmarks(ctx context.Context, o ListOpts) (Page[BenchmarkRun], error) {
	q := newQuery(o).str("deployment", o.Deployment).str("cluster", o.Cluster).
		str("harness", o.Harness).str("phase", o.Phase).str("actor", o.Actor).
		str("request", o.Request).at("since", o.Since)
	return list[BenchmarkRun](ctx, c, "/v1/benchmarks", q.values(), o)
}

func (c *Client) Benchmark(ctx context.Context, ref string) (BenchmarkRun, error) {
	return one[BenchmarkRun](ctx, c, "/v1/benchmarks", ref, nil)
}

// BenchmarkVerdict grades one run, now, against a pinned criteria version.
// Nothing is stored: `criteria` other than the run's own answers "would this
// have passed under the new rules", which a stored verdict cannot answer at all.
func (c *Client) BenchmarkVerdict(ctx context.Context, ref, criteria string) (BenchmarkVerdict, error) {
	var out BenchmarkVerdict
	q := url.Values{}
	if criteria != "" {
		q.Set("criteria", criteria)
	}
	err := c.get(ctx, "/v1/benchmarks/"+url.PathEscape(ref)+"/verdict", q, &out)
	return out, err
}

// BenchmarkRequest reads one ask. There is deliberately no listing of requests
// on the plane: the runs are the collection, and `--request` filters them.
func (c *Client) BenchmarkRequest(ctx context.Context, ref string) (BenchmarkRequest, error) {
	return one[BenchmarkRequest](ctx, c, "/v1/benchmarks/requests", ref, nil)
}

// CreateBenchmarkRequest posts what to measure and at which level. It is a
// separate collection from /v1/benchmarks because a request and a run are
// different objects: one is the ask, the other is a single execution of it, and
// a sweep has one of the first and six of the second.
func (c *Client) CreateBenchmarkRequest(ctx context.Context, req BenchmarkRequestCreate) (BenchmarkRequest, error) {
	var out BenchmarkRequest
	err := c.send(ctx, http.MethodPost, "/v1/benchmarks/requests", nil, req, nil, &out)
	return out, err
}

func (c *Client) BenchmarkRequestVerdict(ctx context.Context, ref, criteria string) (BenchmarkVerdict, error) {
	var out BenchmarkVerdict
	q := url.Values{}
	if criteria != "" {
		q.Set("criteria", criteria)
	}
	err := c.get(ctx, "/v1/benchmarks/requests/"+url.PathEscape(ref)+"/verdict", q, &out)
	return out, err
}

// BenchmarkRequestBands re-aggregates a request's runs by the input-length band
// each landed in. The standard sets a time-to-first-token target per band, and
// one aggregate over a whole sweep evidences none of them.
func (c *Client) BenchmarkRequestBands(ctx context.Context, ref string) ([]BenchmarkBand, error) {
	page, err := list[BenchmarkBand](ctx, c, "/v1/benchmarks/requests/"+url.PathEscape(ref)+"/bands", nil, ListOpts{})
	return page.Items, err
}

// BenchmarkHarnesses is what can be run and what it accepts. Asked of the plane
// rather than compiled in, so a flag the plane stopped accepting stops being
// offered without a CLI release.
func (c *Client) BenchmarkHarnesses(ctx context.Context) ([]BenchmarkHarness, error) {
	page, err := list[BenchmarkHarness](ctx, c, "/v1/benchmarks/harnesses", nil, ListOpts{})
	return page.Items, err
}

// CreateBenchmark starts a run. It creates two things on the plane — the record
// and the operation that carries it out — and returns the record; the operation
// is named on it and is where cancellation lives.
func (c *Client) CreateBenchmark(ctx context.Context, req BenchmarkRunCreate) (BenchmarkRun, error) {
	var out BenchmarkRun
	err := c.send(ctx, http.MethodPost, "/v1/benchmarks", nil, req, nil, &out)
	return out, err
}

func (c *Client) Components(ctx context.Context, o ListOpts) (Page[Component], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("name", o.Name).str("kind", o.Kind).
		boolean("behind", o.Behind).boolean("enabled", o.Enabled).str("search", o.Search)
	return list[Component](ctx, c, "/v1/components", q.values(), o)
}

func (c *Client) Component(ctx context.Context, ref string) (Component, error) {
	return one[Component](ctx, c, "/v1/components", ref, nil)
}

func (c *Client) Events(ctx context.Context, o ListOpts) (Page[Event], error) {
	q := newQuery(o).str("cluster", o.Cluster).str("subject", o.Subject).str("kind", o.Kind).
		str("severity", o.Severity).at("since", o.Since).at("until", o.Until)
	return list[Event](ctx, c, "/v1/events", q.values(), o)
}

func (c *Client) Event(ctx context.Context, ref string) (Event, error) {
	return one[Event](ctx, c, "/v1/events", ref, nil)
}

func (c *Client) AlertThresholds(ctx context.Context, o ListOpts) (Page[AlertThreshold], error) {
	q := newQuery(o).str("rule", o.Rule).str("subject_type", o.Kind).str("subject", o.Subject).
		boolean("enabled", o.Enabled).boolean("fleet_default", o.FleetDefault)
	return list[AlertThreshold](ctx, c, "/v1/alert-thresholds", q.values(), o)
}

func (c *Client) AlertThreshold(ctx context.Context, ref string) (AlertThreshold, error) {
	return one[AlertThreshold](ctx, c, "/v1/alert-thresholds", ref, nil)
}

func (c *Client) CreateAlertThreshold(ctx context.Context, req AlertThresholdCreate) (AlertThreshold, error) {
	var out AlertThreshold
	err := c.send(ctx, http.MethodPost, "/v1/alert-thresholds", nil, req, nil, &out)
	return out, err
}

func (c *Client) UpdateAlertThreshold(ctx context.Context, ref string, req AlertThresholdUpdate) (AlertThreshold, error) {
	var out AlertThreshold
	err := c.send(ctx, http.MethodPatch, "/v1/alert-thresholds/"+url.PathEscape(ref), nil, req, nil, &out)
	return out, err
}

func (c *Client) DeleteAlertThreshold(ctx context.Context, ref string) error {
	return c.send(ctx, http.MethodDelete, "/v1/alert-thresholds/"+url.PathEscape(ref), nil, nil, nil, nil)
}

// AlertRules is the catalogue a threshold may name. Its own path rather than a
// segment under /v1/alert-thresholds, so no literal has to win a race against
// {ref}; asked of the plane rather than compiled in, so a metric name never
// becomes a string somebody types.
func (c *Client) AlertRules(ctx context.Context) ([]MetricRule, error) {
	page, err := list[MetricRule](ctx, c, "/v1/alert-rules", nil, ListOpts{})
	return page.Items, err
}

// --- transport -------------------------------------------------------------

func (c *Client) get(ctx context.Context, path string, q url.Values, out any) error {
	return c.send(ctx, http.MethodGet, path, q, nil, nil, out)
}

func (c *Client) send(ctx context.Context, method, path string, q url.Values, body any, headers map[string]string, out any) error {
	u := c.Server + path
	if len(q) > 0 {
		u += "?" + q.Encode()
	}

	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return errs.Wrap(err, errs.CodeInternal, "cannot encode request body")
		}
		reader = bytes.NewReader(encoded)
	}

	req, err := http.NewRequestWithContext(ctx, method, u, reader)
	if err != nil {
		return errs.Wrap(err, errs.CodeInternal, "cannot build request")
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "hgr/"+c.Version)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	if c.Scope != "" {
		req.Header.Set("X-Hangar-Project", c.Scope)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}

	resp, err := c.HTTP.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return errs.Wrap(err, errs.CodeTimeout, "request to %s timed out", c.Server)
		}
		return errs.Wrap(err, errs.CodeNetwork, "cannot reach the control plane at %s", c.Server).
			WithFix("Run offline against the fake backend", "hgr --fake cluster ls")
	}
	defer resp.Body.Close()

	payload, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode >= 400 {
		return httpError(resp, payload)
	}
	if out == nil || resp.StatusCode == http.StatusNoContent {
		return nil
	}
	if err := json.Unmarshal(payload, out); err != nil {
		return errs.Wrap(err, errs.CodeServerError, "control plane returned a response the CLI cannot decode")
	}
	return nil
}

// wireError is the plane's error body: `code` is the contract and is what the
// CLI branches on, `message` is for people, and `detail` is an object.
type wireError struct {
	Code    string         `json:"code"`
	Message string         `json:"message"`
	Detail  map[string]any `json:"detail"`
}

// planeCodes maps the plane's error vocabulary onto the CLI's. The plane's
// `invalid` is a category error rather than a validation failure and answers
// 409, so it maps to INVALID_STATE and not USAGE.
var planeCodes = map[string]errs.Code{
	"not_found":       errs.CodeNotFound,
	"conflict":        errs.CodeConflict,
	"invalid":         errs.CodeInvalidState,
	"invalid_request": errs.CodeUsage,
	"not_authorized":  errs.CodeAuthRequired,
	"forbidden":       errs.CodeForbidden,
	"internal":        errs.CodeServerError,
	"http_error":      errs.CodeServerError,
}

func httpError(resp *http.Response, body []byte) error {
	var we wireError
	_ = json.Unmarshal(body, &we)
	msg := we.Message
	if msg == "" {
		msg = strings.TrimSpace(string(body))
	}
	if msg == "" {
		msg = http.StatusText(resp.StatusCode)
	}

	code := errs.CodeServerError
	switch resp.StatusCode {
	case http.StatusBadRequest, http.StatusUnprocessableEntity:
		code = errs.CodeUsage
	case http.StatusUnauthorized:
		code = errs.CodeAuthRequired
	case http.StatusForbidden:
		code = errs.CodeForbidden
	case http.StatusNotFound:
		code = errs.CodeNotFound
	case http.StatusConflict:
		code = errs.CodeConflict
	case http.StatusTooManyRequests:
		code = errs.CodeRateLimited
	}
	if mapped, ok := planeCodes[we.Code]; ok {
		code = mapped
	}

	e := errs.New(code, "%s", msg).WithCausef("HTTP %d from %s", resp.StatusCode, resp.Request.URL.Path)
	if len(we.Detail) > 0 {
		e = e.WithCause(detailString(we.Detail))
	}
	if code == errs.CodeRateLimited {
		if s := resp.Header.Get("Retry-After"); s != "" {
			if n, err := strconv.Atoi(s); err == nil {
				e = e.WithRetry(n)
			}
		}
	}
	return e
}

func detailString(detail map[string]any) string {
	keys := make([]string, 0, len(detail))
	for k := range detail {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s=%v", k, detail[k]))
	}
	return strings.Join(parts, " ")
}

// ParseDuration accepts Go durations plus a bare number of seconds.
func ParseDuration(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if n, err := strconv.Atoi(s); err == nil {
		return time.Duration(n) * time.Second, nil
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		return 0, errs.New(errs.CodeUsage, "%q is not a duration", s).
			WithCause("use a Go duration such as 30s, 5m, 1h, or a bare number of seconds")
	}
	return d, nil
}

// ParseKV turns repeated KEY=VALUE flags into a map.
func ParseKV(pairs []string, what string) (map[string]string, error) {
	if len(pairs) == 0 {
		return nil, nil
	}
	out := make(map[string]string, len(pairs))
	for _, p := range pairs {
		k, v, ok := strings.Cut(p, "=")
		if !ok || strings.TrimSpace(k) == "" {
			return nil, errs.New(errs.CodeUsage, "%s %q is not KEY=VALUE", what, p)
		}
		out[k] = v
	}
	return out, nil
}

// Age renders a duration since t the way every listing prints it.
func Age(t time.Time) string {
	if t.IsZero() {
		return "-"
	}
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return fmt.Sprintf("%ds", int(d.Seconds()))
	case d < time.Hour:
		return fmt.Sprintf("%dm", int(d.Minutes()))
	case d < 48*time.Hour:
		return fmt.Sprintf("%dh", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd", int(d.Hours()/24))
	}
}
