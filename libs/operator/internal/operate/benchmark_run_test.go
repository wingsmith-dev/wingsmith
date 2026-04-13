package operate

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

const (
	benchID    = "bmr_01j9x2cccccccccccccccccccc"
	benchNS    = "dynamo"
	benchModel = "meta-llama/Llama-3.1-8B"
)

// bothRecorder keeps the two frame kinds apart: an operation reports that the errand finished, and
// the run's measurement travels separately on benchmark.observed.
type bothRecorder struct {
	mu         sync.Mutex
	operations []wire.OperationObserved
	benchmarks []wire.BenchmarkObserved
	sendErr    error
}

func (r *bothRecorder) Send(t syncpkg.MessageType, _ string, payload any) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	switch p := payload.(type) {
	case wire.OperationObserved:
		r.operations = append(r.operations, p)
	case wire.BenchmarkObserved:
		r.benchmarks = append(r.benchmarks, p)
	}
	_ = t
	return r.sendErr
}

func (r *bothRecorder) lastOperation() (wire.OperationObserved, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.operations) == 0 {
		return wire.OperationObserved{}, false
	}
	return r.operations[len(r.operations)-1], true
}

func (r *bothRecorder) lastBenchmark() (wire.BenchmarkObserved, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.benchmarks) == 0 {
		return wire.BenchmarkObserved{}, false
	}
	return r.benchmarks[len(r.benchmarks)-1], true
}

// stubLogs stands in for the pods/log subresource, which controller-runtime's client cannot serve.
type stubLogs struct {
	out string
	err error
}

func (s stubLogs) PodLogs(_ context.Context, _, _ string) (string, error) { return s.out, s.err }

func benchmarkOperation(params map[string]string) wire.OperationDesired {
	merged := map[string]string{
		"benchmark_id": benchID,
		"harness":      "aiperf",
		"image":        "ghcr.io/hangar-dev/aiperf:0.4.1",
		"model":        benchModel,
	}
	for k, v := range params {
		if v == "" {
			delete(merged, k)
			continue
		}
		merged[k] = v
	}
	return wire.OperationDesired{
		OperationID: "opr_01j9x2bbbbbbbbbbbbbbbbbbbb",
		ClusterID:   testCluster,
		Kind:        "benchmark",
		Target:      wire.OperationTarget{Kind: "deployment", ID: "dep_01j9x2dddddddddddddddddddd"},
		Parameters:  merged,
	}
}

// finishedJob is the job the renderer would have produced, already complete. startJob adopts an
// existing job rather than recreating it, so pre-creating it is exactly the restart-mid-run case.
func finishedJob(condition batchv1.JobConditionType, message string) *batchv1.Job {
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Namespace: benchNS, Name: render.BenchmarkJobName(benchID)},
		Status: batchv1.JobStatus{
			Conditions: []batchv1.JobCondition{
				{Type: condition, Status: corev1.ConditionTrue, Message: message},
			},
		},
	}
}

func benchmarkPod(name string, created time.Time) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:         benchNS,
			Name:              name,
			Labels:            map[string]string{"hangar.dev/benchmark": benchID},
			CreationTimestamp: metav1.NewTime(created),
		},
	}
}

func runBenchmark(t *testing.T, logs LogReader, op wire.OperationDesired, objects ...client.Object) (*bothRecorder, client.Client) {
	t.Helper()
	c := fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithObjects(objects...).Build()
	rec := &bothRecorder{}
	e := New(&Executor{
		Client:    c,
		ClusterID: testCluster,
		Log:       discardLogger(),
		Render:    render.Options{Namespace: benchNS},
		Benchmark: render.BenchmarkOptions{Endpoint: "http://frontend.dynamo.svc.cluster.local:8000"},
		Logs:      logs,
	})
	e.Bind(rec)

	payload, err := json.Marshal(op)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := e.Handle(context.Background(), &syncpkg.Envelope{Type: TypeOperationDesired, Payload: payload}); err != nil {
		t.Fatalf("handle: %v", err)
	}

	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if last, ok := rec.lastOperation(); ok && (last.Phase == "succeeded" || last.Phase == "failed") {
			return rec, c
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("benchmark operation did not reach a terminal phase")
	return nil, nil
}

// TestABenchmarkRunReportsOnTwoFrames is the shape the package doc describes: the operation says
// the errand finished, and the measurement travels on benchmark.observed because
// operation.observed.result is a flat string map and every harness emits nested JSON.
func TestABenchmarkRunReportsOnTwoFrames(t *testing.T) {
	logs := stubLogs{out: framed("aiperf 0.4.1", `{"time_to_first_token":{"avg":182.4}}`)}
	rec, _ := runBenchmark(t, logs, benchmarkOperation(nil),
		finishedJob(batchv1.JobComplete, ""),
		benchmarkPod("bench-abc", time.Now()),
	)

	op, ok := rec.lastOperation()
	if !ok || op.Phase != "succeeded" {
		t.Fatalf("operation phase = %+v, want succeeded", op)
	}
	bench, ok := rec.lastBenchmark()
	if !ok {
		t.Fatal("no benchmark.observed frame was sent; the run is pending in the plane forever")
	}
	if bench.Phase != "succeeded" {
		t.Errorf("benchmark phase = %q, want succeeded", bench.Phase)
	}
	if bench.BenchmarkID != benchID || bench.ClusterID != testCluster {
		t.Errorf("benchmark frame is not identified: %+v", bench)
	}
	if !strings.Contains(string(bench.Summary), "time_to_first_token") {
		t.Errorf("the harness export did not reach the frame: %q", string(bench.Summary))
	}
	if bench.StartedAt == nil || bench.CompletedAt == nil {
		t.Error("the run reports no interval")
	}
}

// TestAFailedBenchmarkStillReports is the invariant the deferred report exists for: failing
// silently would leave the run pending in the plane forever with nothing saying why.
func TestAFailedBenchmarkStillReports(t *testing.T) {
	rec, _ := runBenchmark(t, stubLogs{out: ""}, benchmarkOperation(nil),
		finishedJob(batchv1.JobFailed, "harness exited 1"),
		benchmarkPod("bench-abc", time.Now()),
	)

	op, _ := rec.lastOperation()
	if op.Phase != "failed" {
		t.Fatalf("operation phase = %q, want failed", op.Phase)
	}
	bench, ok := rec.lastBenchmark()
	if !ok {
		t.Fatal("a failed run sent no benchmark.observed frame")
	}
	if bench.Phase != "failed" || bench.Error == "" {
		t.Errorf("a failed run must say why: %+v", bench)
	}
	if !strings.Contains(bench.Error, "harness exited 1") {
		t.Errorf("the job's own message did not survive into the frame: %q", bench.Error)
	}
}

// TestTheLogIsReadEvenWhenTheJobFailed: a run that hit its deadline after most of its requests
// still measured those requests, and the export it wrote is the only evidence about what happened.
func TestTheLogIsReadEvenWhenTheJobFailed(t *testing.T) {
	logs := stubLogs{out: framed("aiperf 0.4.1", `{"request_count":812}`)}
	rec, _ := runBenchmark(t, logs, benchmarkOperation(nil),
		finishedJob(batchv1.JobFailed, "deadline exceeded"),
		benchmarkPod("bench-abc", time.Now()),
	)

	bench, ok := rec.lastBenchmark()
	if !ok {
		t.Fatal("no benchmark frame")
	}
	if !strings.Contains(string(bench.Summary), "request_count") {
		t.Errorf("the partial measurement was discarded: %q", string(bench.Summary))
	}
}

// TestTheNewestBenchmarkPodWins: with BackoffLimit at zero there is normally one pod, but a node
// failure can produce a second, and the later one is the attempt that has something to say.
func TestTheNewestBenchmarkPodWins(t *testing.T) {
	var asked string
	logs := logReaderFunc(func(_ context.Context, _, name string) (string, error) {
		asked = name
		return framed("aiperf 0.4.1", `{"ok":true}`), nil
	})
	now := time.Now()
	runBenchmark(t, logs, benchmarkOperation(nil),
		finishedJob(batchv1.JobComplete, ""),
		benchmarkPod("bench-first", now.Add(-10*time.Minute)),
		benchmarkPod("bench-retry", now),
	)
	if asked != "bench-retry" {
		t.Fatalf("read the log of %q, want the most recent attempt bench-retry", asked)
	}
}

// TestABenchmarkWithNoPodFailsRatherThanReportingNothing.
func TestABenchmarkWithNoPodFailsRatherThanReportingNothing(t *testing.T) {
	rec, _ := runBenchmark(t, stubLogs{}, benchmarkOperation(nil), finishedJob(batchv1.JobComplete, ""))

	bench, ok := rec.lastBenchmark()
	if !ok {
		t.Fatal("no benchmark frame")
	}
	// The specific reason — no pod — is logged as a warning and does not reach the frame; the
	// plane is told only that nothing was found. See TestAReadFailureOnTheLogIsReported.
	if bench.Phase != "failed" || !strings.Contains(bench.Error, "wrote no result") {
		t.Errorf("a run whose log is unreachable must say so: %+v", bench)
	}
}

// TestAnOperatorThatCannotReadLogsRefusesUpFront rather than running a job whose result it could
// never collect and charging the cluster for it.
func TestAnOperatorThatCannotReadLogsRefusesUpFront(t *testing.T) {
	rec, c := runBenchmark(t, nil, benchmarkOperation(nil))

	bench, ok := rec.lastBenchmark()
	if !ok {
		t.Fatal("no benchmark frame")
	}
	if bench.Phase != "failed" || !strings.Contains(bench.Error, "cannot read job logs") {
		t.Errorf("failure does not name the reason: %+v", bench)
	}
	var job batchv1.Job
	err := c.Get(context.Background(), types.NamespacedName{Namespace: benchNS, Name: render.BenchmarkJobName(benchID)}, &job)
	if err == nil {
		t.Error("a job was created by an operator that could not have read its result")
	}
}

// TestABenchmarkWithNoIDIsRefusedBeforeAnythingIsReported: without an id there is nothing to
// report the result against, so this is the one refusal that precedes the deferred report.
func TestABenchmarkWithNoIDIsRefusedBeforeAnythingIsReported(t *testing.T) {
	rec, _ := runBenchmark(t, stubLogs{}, benchmarkOperation(map[string]string{"benchmark_id": ""}))

	op, _ := rec.lastOperation()
	if op.Phase != "failed" {
		t.Fatalf("operation phase = %q, want failed", op.Phase)
	}
	if _, ok := rec.lastBenchmark(); ok {
		t.Error("a run with no id reported a benchmark frame that names nothing")
	}
}

// TestABenchmarkAimedAtANodeIsRefused: the target kind is what says which object is under test,
// and a node is not something this harness can drive load at.
func TestABenchmarkAimedAtANodeIsRefused(t *testing.T) {
	op := benchmarkOperation(nil)
	op.Target = wire.OperationTarget{Kind: "node", ID: "nod_01j9x2eeeeeeeeeeeeeeeeeeee"}
	rec, _ := runBenchmark(t, stubLogs{}, op)

	last, _ := rec.lastOperation()
	if last.Phase != "failed" {
		t.Fatalf("operation phase = %q, want failed", last.Phase)
	}
	if last.Failure == nil || !strings.Contains(last.Failure.Message, "deployment target") {
		t.Errorf("failure does not name the mismatch: %+v", last.Failure)
	}
}

// TestAnUnrenderableBenchmarkFailsBeforeCreatingAJob.
func TestAnUnrenderableBenchmarkFailsBeforeCreatingAJob(t *testing.T) {
	rec, c := runBenchmark(t, stubLogs{}, benchmarkOperation(map[string]string{"harness": "vllm-bench"}))

	bench, ok := rec.lastBenchmark()
	if !ok {
		t.Fatal("no benchmark frame")
	}
	if bench.Phase != "failed" {
		t.Errorf("phase = %q, want failed", bench.Phase)
	}
	var job batchv1.Job
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: benchNS, Name: render.BenchmarkJobName(benchID)}, &job); err == nil {
		t.Error("a job was created for a harness this operator cannot render")
	}
}

// TestAReadFailureOnTheLogFailsTheRunButNotWithItsReason pins current behaviour, and names the
// gap in it: the read error is logged as a warning and the frame carries the generic "wrote no
// result". A run denied pods/log by RBAC and a run whose harness printed nothing are therefore
// indistinguishable in the plane, which sends whoever is debugging it to the wrong cluster.
//
// TODO: carry logErr into observed.Error when the summary is empty.
func TestAReadFailureOnTheLogFailsTheRunButNotWithItsReason(t *testing.T) {
	logs := stubLogs{err: errors.New("pods/log is forbidden")}
	rec, _ := runBenchmark(t, logs, benchmarkOperation(nil),
		finishedJob(batchv1.JobComplete, ""),
		benchmarkPod("bench-abc", time.Now()),
	)

	bench, _ := rec.lastBenchmark()
	if bench.Phase != "failed" {
		t.Fatalf("phase = %q, want failed", bench.Phase)
	}
	if strings.Contains(bench.Error, "forbidden") {
		t.Log("the read error now reaches the frame; delete the TODO above and tighten this test")
	}
}

// logReaderFunc adapts a function to LogReader.
type logReaderFunc func(ctx context.Context, namespace, name string) (string, error)

func (f logReaderFunc) PodLogs(ctx context.Context, namespace, name string) (string, error) {
	return f(ctx, namespace, name)
}

// TestARestartedOperatorAdoptsTheJobItAlreadyStarted. The one case that produces an existing job
// is an operator that restarted mid-run and was replayed the operation; deleting and starting
// again would discard a measurement that may be nearly finished and charge the cluster twice.
func TestARestartedOperatorAdoptsTheJobItAlreadyStarted(t *testing.T) {
	existing := finishedJob(batchv1.JobComplete, "")
	existing.Labels = map[string]string{"adopted-me": "yes"}
	logs := stubLogs{out: framed("aiperf 0.4.1", `{"ok":true}`)}

	rec, c := runBenchmark(t, logs, benchmarkOperation(nil), existing, benchmarkPod("bench-abc", time.Now()))

	if bench, _ := rec.lastBenchmark(); bench.Phase != "succeeded" {
		t.Fatalf("phase = %q, want succeeded", bench.Phase)
	}
	var after batchv1.Job
	if err := c.Get(context.Background(),
		types.NamespacedName{Namespace: benchNS, Name: render.BenchmarkJobName(benchID)}, &after); err != nil {
		t.Fatalf("read job: %v", err)
	}
	if after.Labels["adopted-me"] != "yes" {
		t.Error("the existing job was replaced rather than adopted; its measurement was thrown away")
	}
}

// TestAJobThatCannotBeReadFailsTheRun rather than being taken for one that is not there yet and
// created a second time.
func TestAJobThatCannotBeReadFailsTheRun(t *testing.T) {
	boom := interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*batchv1.Job); ok {
				return errors.New("apiserver unreachable")
			}
			return c.Get(ctx, key, obj, opts...)
		},
	}
	c := fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithInterceptorFuncs(boom).Build()
	rec := &bothRecorder{}
	e := New(&Executor{
		Client:    c,
		ClusterID: testCluster,
		Log:       discardLogger(),
		Render:    render.Options{Namespace: benchNS},
		Benchmark: render.BenchmarkOptions{Endpoint: "http://frontend:8000"},
		Logs:      stubLogs{},
	})
	e.Bind(rec)

	payload, _ := json.Marshal(benchmarkOperation(nil))
	if err := e.Handle(context.Background(), &syncpkg.Envelope{Type: TypeOperationDesired, Payload: payload}); err != nil {
		t.Fatalf("handle: %v", err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if bench, ok := rec.lastBenchmark(); ok {
			if bench.Phase != "failed" || !strings.Contains(bench.Error, "read benchmark job") {
				t.Fatalf("failure does not name the read: %+v", bench)
			}
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("an unreadable job did not fail the run")
}

// TestABenchmarkDeadlineEndsTheWait. awaitJob polls, so a job that never reaches a condition must
// be bounded by the operator's own timeout or the run would hold a goroutine forever.
func TestABenchmarkDeadlineEndsTheWait(t *testing.T) {
	pending := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Namespace: benchNS, Name: render.BenchmarkJobName(benchID)},
		Status:     batchv1.JobStatus{Active: 1},
	}
	c := fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).
		WithObjects(pending, benchmarkPod("bench-abc", time.Now())).Build()
	rec := &bothRecorder{}
	e := New(&Executor{
		Client:           c,
		ClusterID:        testCluster,
		Log:              discardLogger(),
		Render:           render.Options{Namespace: benchNS},
		Benchmark:        render.BenchmarkOptions{Endpoint: "http://frontend:8000"},
		Logs:             stubLogs{out: framed("aiperf 0.4.1", `{"partial":true}`)},
		BenchmarkTimeout: 50 * time.Millisecond,
	})
	e.Bind(rec)

	payload, _ := json.Marshal(benchmarkOperation(nil))
	if err := e.Handle(context.Background(), &syncpkg.Envelope{Type: TypeOperationDesired, Payload: payload}); err != nil {
		t.Fatalf("handle: %v", err)
	}
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if bench, ok := rec.lastBenchmark(); ok {
			if bench.Phase != "failed" || !strings.Contains(bench.Error, "did not finish in time") {
				t.Fatalf("a run past its deadline reported %+v", bench)
			}
			// What it did measure before the deadline still travels.
			if !strings.Contains(string(bench.Summary), "partial") {
				t.Error("the partial measurement was discarded at the deadline")
			}
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("the benchmark timeout never fired")
}

// TestAnOperationDeadlineAlsoBoundsABenchmark — the wire-level deadline arms the same way.
func TestAnOperationDeadlineAlsoBoundsABenchmark(t *testing.T) {
	pending := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Namespace: benchNS, Name: render.BenchmarkJobName(benchID)},
		Status:     batchv1.JobStatus{Active: 1},
	}
	op := benchmarkOperation(nil)
	op.Deadline = time.Now().Add(50 * time.Millisecond).UTC().Format(time.RFC3339)

	rec, _ := runBenchmark(t, stubLogs{out: ""}, op, pending, benchmarkPod("bench-abc", time.Now()))
	last, _ := rec.lastOperation()
	if last.Phase != "failed" {
		t.Fatalf("phase = %q, want failed at the deadline", last.Phase)
	}
}
