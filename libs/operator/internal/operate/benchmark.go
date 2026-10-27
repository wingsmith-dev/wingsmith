package operate

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// TypeBenchmarkObserved is the frame a run's result comes back on.
const TypeBenchmarkObserved = "benchmark.observed"

// benchmarkPoll is how often a running job is re-read. A benchmark takes minutes; a tighter loop
// would only add API server traffic to the cluster being measured, which is the one cluster where
// added load changes the answer.
const benchmarkPoll = 5 * time.Second

// logTailCap is the largest amount of a run's stdout that crosses the wire. Sized so a typical
// harness's last page of errors and progress is there in full, and so `benchmark.observed` stays
// a frame the hub can carry unfragmented: a run that wants its full log preserved publishes
// under ArtifactsURI instead.
const logTailCap = 64 * 1024

// tailBytes returns the last n bytes of s, snapped to a newline so the first visible line is
// whole. Empty input returns empty instead of a sentinel, as the field is omitempty on the wire.
func tailBytes(s string, n int) string {
	if len(s) <= n {
		return s
	}
	tail := s[len(s)-n:]
	if i := strings.IndexByte(tail, '\n'); i >= 0 && i+1 < len(tail) {
		tail = tail[i+1:]
	}
	return tail
}

// LogReader reads a pod's logs back.
//
// Its own interface, and narrow, because it is the one thing in this operator that the
// controller-runtime client cannot do: logs are a subresource served as a stream, not an object,
// so they need a typed client-go client. Keeping the surface to one method means the executor is
// still testable without one.
type LogReader interface {
	PodLogs(ctx context.Context, namespace, name string) (string, error)
}

// benchmark measures a deployment by running a load generator against this cluster's own frontend.
//
// The work is a Job and not an in-process HTTP loop. A load generator has to saturate an inference
// endpoint, and an operator that did that from its own goroutines would be spending the memory and
// CPU of its reconcile loops on it: the process responsible for reporting the cluster's health
// would be the process making it look unhealthy. It also means the harness is whatever image the
// plane named, so adding vLLM's benchmark script later is a case in the renderer instead of a
// dependency here.
//
// The result travels on `benchmark.observed` and the operation reports only that the errand
// finished. Two frames because `operation.observed.result` is a flat string map and every harness
// emits nested JSON.
//
// A run that fails still reports. Failing silently would leave the run pending in the plane
// forever with nothing saying why, which is the exact failure mode operations exist to avoid.
func (e *Executor) benchmark(ctx context.Context, op wire.OperationDesired) (result map[string]string, err error) {
	benchmarkID := op.Parameters["benchmark_id"]
	if benchmarkID == "" {
		return nil, fmt.Errorf("benchmark operation %s carries no benchmark id", op.OperationID)
	}
	if op.Target.Kind != "deployment" {
		return nil, fmt.Errorf("expected a deployment target, got %q", op.Target.Kind)
	}

	started := time.Now().UTC()
	observed := wire.BenchmarkObserved{
		BenchmarkID:  benchmarkID,
		ClusterID:    e.ClusterID,
		DeploymentID: op.Target.ID,
		Harness:      op.Parameters["harness"],
		StartedAt:    stamp(started),
	}

	// Whatever happens below, the plane is told. The deferred report is the only path that
	// writes the run's terminal phase, so an early return above the defer would be a run left
	// pending, which is why nothing between here and the end returns before it is armed.
	defer func() {
		observed.Phase = "succeeded"
		if err != nil {
			observed.Phase = "failed"
			observed.Error = err.Error()
		}
		completed := time.Now().UTC()
		observed.CompletedAt = stamp(completed)
		observed.ObservedAt = at(completed)
		e.reportBenchmark(benchmarkID, observed)
	}()

	if e.Logs == nil {
		return nil, fmt.Errorf("this operator cannot read job logs and so cannot collect a benchmark result")
	}

	bench := e.Benchmark
	if bench.Endpoint == "" {
		endpoint, lookupErr := e.frontendEndpoint(ctx, op.Target.ID)
		if lookupErr != nil {
			return nil, lookupErr
		}
		bench.Endpoint = endpoint
	}

	job, renderErr := render.BenchmarkJob(benchmarkID, op.Parameters, e.Render, bench)
	if renderErr != nil {
		return nil, renderErr
	}
	if err := e.startJob(ctx, job); err != nil {
		return nil, err
	}

	e.report(op, running(op, started, 0.05, "benchmark job created"))

	if e.BenchmarkTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, e.BenchmarkTimeout)
		defer cancel()
	}

	jobErr := e.awaitJob(ctx, op, job.Namespace, job.Name, started)

	// The log is read whether or not the job succeeded. A run that hit its deadline after most
	// of its requests still measured those requests, and the export it wrote is the only
	// evidence about what happened: discarding it because the process exited non-zero would
	// throw away the more useful half of a failure.
	logs, logErr := e.jobLogs(ctx, job.Namespace, benchmarkID)
	if logErr != nil {
		e.Log.Warn("could not read benchmark logs", "benchmark", benchmarkID, "error", logErr)
	}

	summary, version := parseBenchmarkLog(logs)
	observed.Summary = summary
	observed.HarnessVersion = version
	observed.LogTail = tailBytes(logs, logTailCap)
	if base := e.Benchmark.ArtifactBase; base != "" {
		observed.ArtifactsURI = strings.TrimSuffix(base, "/") + "/" + e.ClusterID + "/" + benchmarkID + "/"
	}

	result = map[string]string{
		"benchmark":  benchmarkID,
		"job":        job.Name,
		"harness":    op.Parameters["harness"],
		"summarised": fmt.Sprintf("%t", len(summary) > 0),
	}
	if version != "" {
		result["harness_version"] = version
	}

	if jobErr != nil {
		return result, jobErr
	}
	if len(summary) == 0 {
		return result, fmt.Errorf("the benchmark job finished but wrote no result this operator could find")
	}
	return result, nil
}

// frontendComponentLabel is how Dynamo's operator marks the Service in front of a graph.
//
// The value this is matched against is render.FrontendComponent instead of a second copy of the
// same string, because that name is what this lookup and the serving agent's both find a frontend
// by, and a copy that drifts is a lookup that finds nothing instead of a compile error.
const frontendComponentLabel = "nvidia.com/dynamo-component"

// deploymentIDLabel is the label the operator puts on everything it renders for one deployment,
// and, because Dynamo copies a component's pod-template labels onto the Service it derives, the
// label that says which deployment a frontend Service belongs to.
const deploymentIDLabel = "hangar.dev/deployment-id"

// frontendEndpoint finds the OpenAI-compatible frontend to aim a run at.
//
// Frontend Service names vary between clusters, so the operator selects on Dynamo's own component
// label instead of guessing a name. `HANGAR_INFERENCE_ENDPOINT` wins when it is set.
//
// With several frontends, the deployment id decides: Dynamo copies a component's pod-template
// labels onto the Service it derives, so a frontend rendered for one deployment carries its id,
// and the run measures the frontend the model is actually served through. A single unlabelled
// frontend, which the chart's shared mode installs, is the fallback. Several frontends with none
// belonging to this deployment is refused, since picking one would attribute a measurement to a
// deployment the load never reached.
func (e *Executor) frontendEndpoint(ctx context.Context, deploymentID string) (string, error) {
	var services corev1.ServiceList
	if err := e.Client.List(ctx, &services, client.InNamespace(e.Render.Namespace)); err != nil {
		return "", fmt.Errorf("look for the inference frontend in %s: %w", e.Render.Namespace, err)
	}

	var found, mine, shared []corev1.Service
	for _, service := range services.Items {
		if service.Spec.Selector[frontendComponentLabel] != render.FrontendComponent {
			continue
		}
		found = append(found, service)
		switch id := frontendDeploymentID(service); {
		case id == "":
			shared = append(shared, service)
		case id == deploymentID && deploymentID != "":
			mine = append(mine, service)
		}
	}

	if len(mine) == 1 {
		return serviceEndpoint(mine[0]), nil
	}
	if len(mine) == 0 && len(shared) == 1 {
		return serviceEndpoint(shared[0]), nil
	}
	if len(found) == 0 {
		return "", fmt.Errorf(
			"no service in %s selects %s=%s, so there is nothing to benchmark against; "+
				"set HANGAR_INFERENCE_ENDPOINT if this cluster fronts its models with something else",
			e.Render.Namespace, frontendComponentLabel, render.FrontendComponent,
		)
	}

	if len(mine) > 1 {
		return "", fmt.Errorf(
			"%s holds %d inference frontends labelled %s=%s (%s) and nothing here can tell them "+
				"apart; set HANGAR_INFERENCE_ENDPOINT",
			e.Render.Namespace, len(mine), deploymentIDLabel, deploymentID, serviceNames(mine),
		)
	}
	return "", fmt.Errorf(
		"%s holds %d inference frontends (%s), none of them labelled %s=%s, so nothing here can "+
			"tell which one serves the deployment under test; set HANGAR_INFERENCE_ENDPOINT",
		e.Render.Namespace, len(found), serviceNames(found), deploymentIDLabel, deploymentID,
	)
}

func serviceNames(services []corev1.Service) string {
	names := make([]string, 0, len(services))
	for _, service := range services {
		names = append(names, service.Name)
	}
	return strings.Join(names, ", ")
}

// frontendDeploymentID reads which deployment a frontend Service belongs to.
//
// The Service's own labels first, because that is where Dynamo copies the pod template's, and its
// selector second: a Service that selects on the deployment id can only reach that deployment's
// pods, so it is no less an answer. Empty means the Service says nothing, which is what the one
// shared frontend a cluster installs looks like.
func frontendDeploymentID(service corev1.Service) string {
	if id := service.Labels[deploymentIDLabel]; id != "" {
		return id
	}
	return service.Spec.Selector[deploymentIDLabel]
}

// serviceEndpoint is the in-cluster URL a load generator aims at.
func serviceEndpoint(service corev1.Service) string {
	port := int32(8000)
	for _, declared := range service.Spec.Ports {
		if declared.Name == "http" || len(service.Spec.Ports) == 1 {
			port = declared.Port
			break
		}
	}
	return fmt.Sprintf("http://%s.%s.svc.cluster.local:%d", service.Name, service.Namespace, port)
}

// startJob creates the run's job, or adopts the one already there.
//
// Adoption, not recreation, because the one case that produces an existing job is an
// operator that restarted mid-run and was replayed the operation. Deleting and starting again
// would discard a measurement that may be nearly finished and charge the cluster for a second one.
func (e *Executor) startJob(ctx context.Context, job *batchv1.Job) error {
	var existing batchv1.Job
	key := types.NamespacedName{Namespace: job.Namespace, Name: job.Name}
	switch err := e.Client.Get(ctx, key, &existing); {
	case err == nil:
		e.Log.Info("benchmark job already exists; waiting on it", "job", job.Name)
		return nil
	case !apierrors.IsNotFound(err):
		return fmt.Errorf("read benchmark job %s: %w", job.Name, err)
	}
	if err := e.Client.Create(ctx, job); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return nil
		}
		return fmt.Errorf("create benchmark job %s: %w", job.Name, err)
	}
	return nil
}

// awaitJob blocks until the job reaches a conclusion, reporting progress as it goes.
func (e *Executor) awaitJob(ctx context.Context, op wire.OperationDesired, namespace, name string, started time.Time) error {
	ticker := time.NewTicker(benchmarkPoll)
	defer ticker.Stop()
	for {
		var job batchv1.Job
		if err := e.Client.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, &job); err != nil {
			return fmt.Errorf("read benchmark job %s: %w", name, err)
		}
		for _, condition := range job.Status.Conditions {
			if condition.Status != corev1.ConditionTrue {
				continue
			}
			switch condition.Type {
			case batchv1.JobComplete:
				return nil
			case batchv1.JobFailed:
				return fmt.Errorf("benchmark job %s failed: %s", name, condition.Message)
			}
		}
		if job.Status.Active > 0 {
			e.report(op, running(op, started, 0.5, "benchmark running"))
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("benchmark job %s did not finish in time: %w", name, ctx.Err())
		case <-ticker.C:
		}
	}
}

// jobLogs finds the pod a run's job created and reads its log.
//
// Matched by the benchmark label, not by the job's selector, because the label is one this
// operator put there and the selector is one the API server generates. The most recent pod wins:
// with BackoffLimit at zero there is normally exactly one, and if a node failure produced a second
// the later one is the attempt that has something to say.
func (e *Executor) jobLogs(ctx context.Context, namespace, benchmarkID string) (string, error) {
	var pods corev1.PodList
	if err := e.Client.List(ctx, &pods,
		client.InNamespace(namespace),
		client.MatchingLabels{"hangar.dev/benchmark": benchmarkID},
	); err != nil {
		return "", fmt.Errorf("list benchmark pods: %w", err)
	}
	if len(pods.Items) == 0 {
		return "", fmt.Errorf("no pod found for benchmark %s", benchmarkID)
	}
	newest := &pods.Items[0]
	for i := range pods.Items {
		if pods.Items[i].CreationTimestamp.After(newest.CreationTimestamp.Time) {
			newest = &pods.Items[i]
		}
	}
	return e.Logs.PodLogs(ctx, newest.Namespace, newest.Name)
}

// parseBenchmarkLog lifts the harness's export and version out of the job's output.
//
// The sentinels are what make this reliable: a load generator prints a human-readable table too,
// and identifying the JSON among it by shape would be guessing at a format the harness is free to
// change. Anything that is not valid JSON between the markers is treated as no result, not
// forwarded, because a truncated export stored as a run's summary is worse than an obvious gap.
func parseBenchmarkLog(logs string) (json.RawMessage, string) {
	return extractSummary(logs), extractVersion(logs)
}

func extractSummary(logs string) json.RawMessage {
	start := strings.Index(logs, render.SummaryBegin)
	if start < 0 {
		return nil
	}
	rest := logs[start+len(render.SummaryBegin):]
	end := strings.Index(rest, render.SummaryEnd)
	if end < 0 {
		return nil
	}
	body := strings.TrimSpace(rest[:end])
	if body == "" || body == "null" {
		return nil
	}
	if !json.Valid([]byte(body)) {
		return nil
	}
	return json.RawMessage(body)
}

func extractVersion(logs string) string {
	start := strings.Index(logs, render.VersionLine)
	if start < 0 {
		return ""
	}
	rest := logs[start+len(render.VersionLine):]
	for _, line := range strings.Split(rest, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, render.SummaryBegin) {
			return ""
		}
		// `aiperf --version` prints "aiperf 0.4.1"; the number is the part worth storing.
		if fields := strings.Fields(line); len(fields) > 1 {
			return fields[len(fields)-1]
		}
		return line
	}
	return ""
}

func (e *Executor) reportBenchmark(benchmarkID string, payload wire.BenchmarkObserved) {
	e.mu.Lock()
	reporter := e.reporter
	e.mu.Unlock()
	if reporter == nil {
		e.Log.Warn("no reporter bound; dropping benchmark result", "benchmark", benchmarkID)
		return
	}
	if err := reporter.Send(TypeBenchmarkObserved, benchmarkID, payload); err != nil {
		e.Log.Error("could not report benchmark result", "benchmark", benchmarkID, "error", err)
	}
}
