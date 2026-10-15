package operate

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/hangar-dev/hangar/libs/operator/internal/observe"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

const testCluster = "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"

// benchImages allowlists the harness image the test operations name.
var benchImages = []string{"ghcr.io/hangar-dev/aiperf:0.4.1"}

// recorder collects what the executor reported instead of sending it.
type recorder struct {
	mu     sync.Mutex
	frames []wire.OperationObserved
}

func (r *recorder) Send(_ syncpkg.MessageType, _ string, payload any) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.frames = append(r.frames, payload.(wire.OperationObserved))
	return nil
}

func (r *recorder) last() wire.OperationObserved {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.frames[len(r.frames)-1]
}

func (r *recorder) phases() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, 0, len(r.frames))
	for _, f := range r.frames {
		out = append(out, f.Phase)
	}
	return out
}

func node(name string, cordoned bool) *corev1.Node {
	return &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec:       corev1.NodeSpec{Unschedulable: cordoned},
	}
}

func acceleratorPod(name, nodeName string) *corev1.Pod {
	pod := unmanagedAcceleratorPod(name, nodeName)
	pod.Labels = map[string]string{render.ManagedByLabel: "hangar-operator"}
	return pod
}

// unmanagedAcceleratorPod holds GPUs but was not placed by this operator.
func unmanagedAcceleratorPod(name, nodeName string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "dynamo", UID: types.UID("uid-" + name)},
		Spec: corev1.PodSpec{
			NodeName: nodeName,
			Containers: []corev1.Container{{
				Name: "main",
				Resources: corev1.ResourceRequirements{
					Limits: corev1.ResourceList{"nvidia.com/gpu": *resource.NewQuantity(2, resource.DecimalSI)},
				},
			}},
		},
	}
}

func plainPod(name, nodeName string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "kube-system"},
		Spec:       corev1.PodSpec{NodeName: nodeName, Containers: []corev1.Container{{Name: "agent"}}},
	}
}

func operation(kind, nodeName string, params map[string]string) wire.OperationDesired {
	return wire.OperationDesired{
		OperationID: "opr_01j9x2kkkkkkkkkkkkkkkkkkkk",
		ClusterID:   testCluster,
		Kind:        kind,
		Target:      wire.OperationTarget{Kind: "node", ID: observe.NodeID(testCluster, nodeName)},
		Parameters:  params,
	}
}

func runOperation(t *testing.T, op wire.OperationDesired, objects ...client.Object) (*recorder, *Executor) {
	t.Helper()
	return runOperationOn(t, false, op, objects...)
}

func runOperationOn(t *testing.T, readOnly bool, op wire.OperationDesired, objects ...client.Object) (*recorder, *Executor) {
	t.Helper()
	builder := fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme)
	for _, o := range objects {
		builder = builder.WithObjects(o)
	}
	c := builder.Build()
	rec := &recorder{}
	e := New(&Executor{Client: c, ClusterID: testCluster, Log: slog.Default(), ReadOnly: readOnly})
	e.Bind(rec)

	payload, err := json.Marshal(op)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	env := &syncpkg.Envelope{Type: TypeOperationDesired, Payload: payload}
	if err := e.Handle(context.Background(), env); err != nil {
		t.Fatalf("handle: %v", err)
	}

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if p := rec.phases(); len(p) > 0 && (p[len(p)-1] == "succeeded" || p[len(p)-1] == "failed") {
			return rec, e
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("operation did not reach a terminal phase; saw %v", rec.phases())
	return nil, nil
}

func TestCordonMarksTheNodeUnschedulable(t *testing.T) {
	rec, e := runOperation(t, operation("cordon", "gpu-0", nil), node("gpu-0", false))

	if got := rec.last().Phase; got != "succeeded" {
		t.Fatalf("phase = %q, want succeeded", got)
	}
	var after corev1.Node
	if err := e.Client.Get(context.Background(), types.NamespacedName{Name: "gpu-0"}, &after); err != nil {
		t.Fatalf("read node: %v", err)
	}
	if !after.Spec.Unschedulable {
		t.Error("node was not cordoned")
	}
}

// One wire kind covers both directions; the parameter says which way.
func TestUncordonIsTheSameKindWithAParameter(t *testing.T) {
	rec, e := runOperation(t, operation("cordon", "gpu-0", map[string]string{"cordon": "false"}), node("gpu-0", true))

	if got := rec.last().Phase; got != "succeeded" {
		t.Fatalf("phase = %q, want succeeded", got)
	}
	var after corev1.Node
	if err := e.Client.Get(context.Background(), types.NamespacedName{Name: "gpu-0"}, &after); err != nil {
		t.Fatalf("read node: %v", err)
	}
	if after.Spec.Unschedulable {
		t.Error("node was not uncordoned")
	}
}

func TestCordoningAnAlreadyCordonedNodeReportsNoChange(t *testing.T) {
	rec, _ := runOperation(t, operation("cordon", "gpu-0", nil), node("gpu-0", true))

	last := rec.last()
	if last.Phase != "succeeded" {
		t.Fatalf("phase = %q, want succeeded", last.Phase)
	}
	if last.Result["changed"] != "false" {
		t.Errorf("changed = %q, want false", last.Result["changed"])
	}
}

// A drain that could not find its node must say so rather than report an empty node emptied.
func TestOperationFailsWhenTheTargetIsNotInThisCluster(t *testing.T) {
	op := operation("drain", "gpu-0", nil)
	op.Target.ID = "nod_01j9x2zzzzzzzzzzzzzzzzzzzz"
	rec, _ := runOperation(t, op, node("gpu-0", false))

	last := rec.last()
	if last.Phase != "failed" {
		t.Fatalf("phase = %q, want failed", last.Phase)
	}
	if last.Failure == nil {
		t.Fatal("a failed operation must carry a failure")
	}
}

// Being asked for something this operator cannot do is a failure, not silence. Silence would leave
// the operation pending until it timed out, and a timeout says the cluster was slow.
func TestAnUnsupportedKindFailsLoudly(t *testing.T) {
	rec, _ := runOperation(t, operation("device_reset", "gpu-0", nil), node("gpu-0", false))

	last := rec.last()
	if last.Phase != "failed" {
		t.Fatalf("phase = %q, want failed", last.Phase)
	}
	if last.Failure == nil || last.Failure.Retryable {
		t.Error("an operation this operator cannot perform is not fixed by retrying it")
	}
}

// A read-only operator refuses, and the refusal is an answer rather than silence.
func TestAReadOnlyOperatorRefusesAndLeavesTheNodeAlone(t *testing.T) {
	rec, e := runOperationOn(t, true, operation("cordon", "gpu-0", nil), node("gpu-0", false))

	last := rec.last()
	if last.Phase != "failed" {
		t.Fatalf("phase = %q, want failed", last.Phase)
	}
	if last.Failure == nil || last.Failure.Retryable {
		t.Error("a read-only operator is not persuaded by a retry")
	}
	var after corev1.Node
	if err := e.Client.Get(context.Background(), types.NamespacedName{Name: "gpu-0"}, &after); err != nil {
		t.Fatalf("read node: %v", err)
	}
	if after.Spec.Unschedulable {
		t.Error("a read-only operator cordoned the node anyway")
	}
}

func TestEveryOperationReportsRunningBeforeItsConclusion(t *testing.T) {
	rec, _ := runOperation(t, operation("cordon", "gpu-0", nil), node("gpu-0", false))

	phases := rec.phases()
	if len(phases) < 2 || phases[0] != "running" {
		t.Errorf("phases = %v, want running first so the plane sees the operation start", phases)
	}
}

// The stream is at-least-once, so the same operation arrives again after any resume. Running a
// drain twice concurrently would report progress against evictions the first pass is still making.
func TestAReplayOfARunningOperationIsDropped(t *testing.T) {
	e := New(&Executor{Client: fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).Build(),
		ClusterID: testCluster, Log: slog.Default()})
	if !e.claim("opr_01j9x2") {
		t.Fatal("the first arrival must be claimed")
	}
	if e.claim("opr_01j9x2") {
		t.Error("a replay of an operation already in flight must be dropped")
	}
	e.release("opr_01j9x2")
	if !e.claim("opr_01j9x2") {
		t.Error("an operation that finished must be claimable again")
	}
}

func TestOnlyAcceleratorPodsAreDrained(t *testing.T) {
	if holdsAccelerator(plainPod("csi-driver", "gpu-0")) {
		t.Error("draining must not evict the agents that make a node reportable")
	}
	if !holdsAccelerator(acceleratorPod("qwen-worker", "gpu-0")) {
		t.Error("a pod holding accelerators is what a drain is for")
	}
	finished := acceleratorPod("prefetch", "gpu-0")
	finished.Status.Phase = corev1.PodSucceeded
	if holdsAccelerator(finished) {
		t.Error("a pod that already completed holds nothing")
	}
}

// ------------------------------------------------------------------ the benchmark lane

// A ramp arrives as one operation per step, all of them within a second of each other. Run
// concurrently they measure each other — every point on the curve carries the load of every other
// point — so the lane runs them in turn, and these tests are about that turn-taking.

func stepBenchmarkID(n int) string {
	return fmt.Sprintf("bmr_01j9x2step%08d", n)
}

func stepPodName(n int) string {
	return fmt.Sprintf("bench-step-%d", n)
}

// stepOperation is one step of a ramp: its own operation, its own benchmark, same deployment.
func stepOperation(n int) wire.OperationDesired {
	op := benchmarkOperation(map[string]string{"benchmark_id": stepBenchmarkID(n)})
	op.OperationID = fmt.Sprintf("opr_01j9x2step%08d", n)
	return op
}

// stepObjects are the job and pod a step would find had it created them, already finished. Being
// adopted rather than created is what keeps these runs fast; the turn-taking under test is the
// same either way.
func stepObjects(n int) []client.Object {
	id := stepBenchmarkID(n)
	return []client.Object{
		&batchv1.Job{
			ObjectMeta: metav1.ObjectMeta{Namespace: benchNS, Name: render.BenchmarkJobName(id)},
			Status: batchv1.JobStatus{Conditions: []batchv1.JobCondition{
				{Type: batchv1.JobComplete, Status: corev1.ConditionTrue},
			}},
		},
		&corev1.Pod{ObjectMeta: metav1.ObjectMeta{
			Namespace: benchNS,
			Name:      stepPodName(n),
			Labels:    map[string]string{"hangar.dev/benchmark": id},
		}},
	}
}

// laneProbe stands in for the log subresource and records what the lane let happen: which runs
// reached the harness, in what order, and how many were inside it at once.
type laneProbe struct {
	mu     sync.Mutex
	active int
	peak   int
	order  []string
	hold   chan struct{}
	linger time.Duration
}

func (p *laneProbe) PodLogs(ctx context.Context, _, name string) (string, error) {
	p.mu.Lock()
	p.active++
	if p.active > p.peak {
		p.peak = p.active
	}
	p.order = append(p.order, name)
	hold, linger := p.hold, p.linger
	p.mu.Unlock()

	var stop error
	if hold != nil {
		select {
		case <-hold:
		case <-ctx.Done():
			stop = ctx.Err()
		}
	} else {
		select {
		case <-time.After(linger):
		case <-ctx.Done():
			stop = ctx.Err()
		}
	}

	p.mu.Lock()
	p.active--
	p.mu.Unlock()
	if stop != nil {
		return "", stop
	}
	return framed("aiperf 0.4.1", `{"ok":true}`), nil
}

func (p *laneProbe) seen() (order []string, peak, active int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.order...), p.peak, p.active
}

func laneExecutor(t *testing.T, probe *laneProbe, depth int, objects ...client.Object) (*Executor, *bothRecorder) {
	t.Helper()
	c := fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithObjects(objects...).Build()
	rec := &bothRecorder{}
	e := New(&Executor{
		Client:         c,
		ClusterID:      testCluster,
		Log:            discardLogger(),
		Render:         render.Options{Namespace: benchNS},
		Benchmark:      render.BenchmarkOptions{Endpoint: "http://frontend.dynamo.svc.cluster.local:8000", Images: benchImages},
		Logs:           probe,
		BenchmarkQueue: depth,
	})
	e.Bind(rec)
	t.Cleanup(e.Close)
	return e, rec
}

func submit(t *testing.T, e *Executor, op wire.OperationDesired) {
	t.Helper()
	payload, err := json.Marshal(op)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := e.Handle(context.Background(), &syncpkg.Envelope{Type: TypeOperationDesired, Payload: payload}); err != nil {
		t.Fatalf("handle: %v", err)
	}
}

// concluded reports the terminal frame for one operation, since a lane carries several at once.
func concluded(rec *bothRecorder, id string) (wire.OperationObserved, bool) {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	for _, frame := range rec.operations {
		if frame.OperationID == id && (frame.Phase == "succeeded" || frame.Phase == "failed") {
			return frame, true
		}
	}
	return wire.OperationObserved{}, false
}

func benchmarked(rec *bothRecorder, id string) bool {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	for _, frame := range rec.benchmarks {
		if frame.BenchmarkID == id {
			return true
		}
	}
	return false
}

func waitFor(t *testing.T, what string, done func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if done() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// TestARampRunsItsStepsOneAtATimeAndInOrder is the bug this lane exists for: six steps arriving
// together used to become six load generators against one deployment, and every step measured
// itself plus the other five.
func TestARampRunsItsStepsOneAtATimeAndInOrder(t *testing.T) {
	const steps = 6
	var objects []client.Object
	for n := 0; n < steps; n++ {
		objects = append(objects, stepObjects(n)...)
	}
	probe := &laneProbe{linger: 30 * time.Millisecond}
	e, rec := laneExecutor(t, probe, 0, objects...)

	accepted := time.Now()
	for n := 0; n < steps; n++ {
		submit(t, e, stepOperation(n))
	}
	// The stream reader must not be held behind a measurement. Handle that waited for the lane
	// would take the whole ramp — six steps of 30ms — so anything under 50ms is decisively an
	// acceptance rather than a wait, with room for a loaded machine.
	if held := time.Since(accepted); held > 50*time.Millisecond {
		t.Errorf("Handle held the sync stream for %s; enqueuing must not wait for the lane", held)
	}

	waitFor(t, "every step to conclude", func() bool {
		for n := 0; n < steps; n++ {
			if _, ok := concluded(rec, stepOperation(n).OperationID); !ok {
				return false
			}
		}
		return true
	})

	order, peak, _ := probe.seen()
	if peak != 1 {
		t.Errorf("%d steps ran at once; a step that overlaps another measures both", peak)
	}
	want := make([]string, 0, steps)
	for n := 0; n < steps; n++ {
		want = append(want, stepPodName(n))
	}
	if len(order) != steps {
		t.Fatalf("the harness ran %d times, want %d: %v", len(order), steps, order)
	}
	for i := range want {
		if order[i] != want[i] {
			t.Fatalf("the ramp ran %v, want the order it was asked for %v", order, want)
		}
	}
}

// TestADrainDoesNotWaitBehindABenchmark. The lane is for benchmarks and nothing else: a
// maintenance window missed for the sake of a measurement would be a worse bug than the one this
// change fixes.
func TestADrainDoesNotWaitBehindABenchmark(t *testing.T) {
	hold := make(chan struct{})
	probe := &laneProbe{hold: hold}
	objects := append(stepObjects(0), node("gpu-0", false))
	e, rec := laneExecutor(t, probe, 0, objects...)

	submit(t, e, stepOperation(0))
	waitFor(t, "the benchmark to reach the harness", func() bool {
		_, _, active := probe.seen()
		return active == 1
	})

	cordon := operation(KindCordon, "gpu-0", nil)
	submit(t, e, cordon)
	waitFor(t, "the cordon to conclude while the benchmark is still running", func() bool {
		_, ok := concluded(rec, cordon.OperationID)
		return ok
	})

	if _, _, active := probe.seen(); active != 1 {
		t.Error("the benchmark finished first; this proves nothing about the cordon overtaking it")
	}
	if frame, _ := concluded(rec, cordon.OperationID); frame.Phase != "succeeded" {
		t.Errorf("cordon phase = %q, want succeeded", frame.Phase)
	}
	close(hold)
}

// TestAFullLaneRefusesOutLoudRatherThanDroppingTheStep. A step that vanished would leave the plane
// waiting for a point on a curve nobody is measuring, with no reason recorded anywhere.
func TestAFullLaneRefusesOutLoudRatherThanDroppingTheStep(t *testing.T) {
	hold := make(chan struct{})
	probe := &laneProbe{hold: hold}
	objects := append(stepObjects(0), stepObjects(1)...)
	e, rec := laneExecutor(t, probe, 1, objects...)

	submit(t, e, stepOperation(0))
	waitFor(t, "the first step to reach the harness", func() bool {
		_, _, active := probe.seen()
		return active == 1
	})
	submit(t, e, stepOperation(1)) // fills the one slot
	refusedStep := stepOperation(2)
	submit(t, e, refusedStep)

	frame, ok := concluded(rec, refusedStep.OperationID)
	if !ok {
		t.Fatal("the refused step reported nothing at all")
	}
	if frame.Phase != "failed" || frame.Failure == nil {
		t.Fatalf("phase = %q, want a failure that says the lane was full", frame.Phase)
	}
	if frame.Failure.Code != "queue_full" || !frame.Failure.Retryable {
		t.Errorf("failure = %+v, want a retryable queue_full", frame.Failure)
	}
	// Refusing it must also let it be asked for again rather than holding the claim for ever.
	if !e.claim(refusedStep.OperationID) {
		t.Error("a refused step stayed claimed and can never be resubmitted")
	}
	close(hold)
}

// TestAQueuedBenchmarkCanBeCancelled. Most of a ramp is in the queue, so a cancellation that
// reached only the running step would let the rest run against a deployment somebody already
// decided to stop measuring.
func TestAQueuedBenchmarkCanBeCancelled(t *testing.T) {
	hold := make(chan struct{})
	probe := &laneProbe{hold: hold}
	objects := append(stepObjects(0), stepObjects(1)...)
	e, rec := laneExecutor(t, probe, 0, objects...)

	submit(t, e, stepOperation(0))
	waitFor(t, "the first step to reach the harness", func() bool {
		_, _, active := probe.seen()
		return active == 1
	})
	queuedStep := stepOperation(1)
	submit(t, e, queuedStep)

	if !e.Cancel(queuedStep.OperationID) {
		t.Fatal("a queued benchmark was not cancellable")
	}
	close(hold)

	waitFor(t, "the cancelled step to conclude", func() bool {
		_, ok := concluded(rec, queuedStep.OperationID)
		return ok
	})
	frame, _ := concluded(rec, queuedStep.OperationID)
	if frame.Phase != "failed" || frame.Failure == nil || frame.Failure.Code != "cancelled" {
		t.Fatalf("frame = %+v, want a failure that says it was cancelled", frame)
	}
	if benchmarked(rec, stepBenchmarkID(1)) {
		t.Error("a cancelled step still ran the harness")
	}
	order, _, _ := probe.seen()
	if len(order) != 1 || order[0] != stepPodName(0) {
		t.Errorf("the harness ran %v; only the step that was already running should have", order)
	}
}

// TestCancellingAnOperationThisExecutorDoesNotHoldSaysSo, rather than reporting a stop it cannot
// make.
func TestCancellingAnOperationThisExecutorDoesNotHoldSaysSo(t *testing.T) {
	e, _ := laneExecutor(t, &laneProbe{}, 0)
	if e.Cancel("opr_01j9x2nothinghere") {
		t.Error("the executor claimed to cancel an operation it never accepted")
	}
}

// TestTheLaneStopsWhenItsContextIsCancelled — one worker started with the executor, and no
// goroutine left behind when the process shuts down.
func TestTheLaneStopsWhenItsContextIsCancelled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	e, _ := laneExecutor(t, &laneProbe{}, 0)
	e.Start(ctx)

	select {
	case <-e.laneDone:
		t.Fatal("the lane stopped before anything cancelled it")
	case <-time.After(20 * time.Millisecond):
	}

	cancel()
	select {
	case <-e.laneDone:
	case <-time.After(5 * time.Second):
		t.Fatal("the lane worker outlived its context")
	}
}

// TestStepsStillWaitingWhenTheLaneStopsAreReported. Shutting down is a reason, and a reason is
// what the plane needs; silence would leave the last steps of a ramp pending for ever.
func TestStepsStillWaitingWhenTheLaneStopsAreReported(t *testing.T) {
	hold := make(chan struct{})
	probe := &laneProbe{hold: hold}
	e, rec := laneExecutor(t, probe, 0, stepObjects(0)...)

	submit(t, e, stepOperation(0))
	waitFor(t, "the first step to reach the harness", func() bool {
		_, _, active := probe.seen()
		return active == 1
	})
	waiting := stepOperation(1)
	submit(t, e, waiting)

	e.Close()
	close(hold)

	frame, ok := concluded(rec, waiting.OperationID)
	if !ok {
		t.Fatal("a step waiting at shutdown was dropped without a word")
	}
	if frame.Failure == nil || frame.Failure.Code != "shutting_down" || !frame.Failure.Retryable {
		t.Errorf("failure = %+v, want a retryable shutting_down", frame.Failure)
	}
}

// TestAReplayOfAQueuedBenchmarkIsDropped: claim does its existing job on the lane too, or a
// resumed stream would put the same step in the queue twice.
func TestAReplayOfAQueuedBenchmarkIsDropped(t *testing.T) {
	hold := make(chan struct{})
	probe := &laneProbe{hold: hold}
	objects := append(append(stepObjects(0), stepObjects(1)...), stepObjects(2)...)
	e, rec := laneExecutor(t, probe, 0, objects...)

	submit(t, e, stepOperation(0))
	waitFor(t, "the first step to reach the harness", func() bool {
		_, _, active := probe.seen()
		return active == 1
	})
	submit(t, e, stepOperation(1))
	submit(t, e, stepOperation(1))
	close(hold)

	// The lane is first in, first out, so once a step submitted after the replay has run, a
	// queued replay would have run before it.
	sentinel := stepOperation(2)
	submit(t, e, sentinel)
	waitFor(t, "the step after the replay to conclude", func() bool {
		_, ok := concluded(rec, sentinel.OperationID)
		return ok
	})
	if order, _, _ := probe.seen(); len(order) != 3 {
		t.Errorf("the harness ran %v; a replay was queued as a second run", order)
	}
}

// TestAStepRunningAtShutdownIsReportedRetryable. The lane's context is the running step's, so
// stopping the operator stops the measurement — and a measurement this operator abandoned is not
// one the cluster cannot make. Reported as an ordinary execution failure it would be a ramp step
// nobody ever asks for again.
func TestAStepRunningAtShutdownIsReportedRetryable(t *testing.T) {
	hold := make(chan struct{})
	defer close(hold)
	probe := &laneProbe{hold: hold}
	e, rec := laneExecutor(t, probe, 0, stepObjects(0)...)

	step := stepOperation(0)
	submit(t, e, step)
	waitFor(t, "the step to reach the harness", func() bool {
		_, _, active := probe.seen()
		return active == 1
	})

	e.Close()

	frame, ok := concluded(rec, step.OperationID)
	if !ok {
		t.Fatal("a step running at shutdown concluded with nothing at all")
	}
	if frame.Phase != "failed" || frame.Failure == nil {
		t.Fatalf("frame = %+v, want a failure saying the operator stopped", frame)
	}
	if frame.Failure.Code != "shutting_down" || !frame.Failure.Retryable {
		t.Errorf("failure = %+v, want a retryable shutting_down, not a measurement declared impossible",
			frame.Failure)
	}
	if frame.StartedAt == nil {
		t.Error("a step that did run must report when it started")
	}
}

// TestARunningBenchmarkCancelledSaysItWasCancelled, and says it in the terms that stop the plane
// asking again: somebody decided to stop measuring, which no retry improves.
func TestARunningBenchmarkCancelledSaysItWasCancelled(t *testing.T) {
	hold := make(chan struct{})
	defer close(hold)
	probe := &laneProbe{hold: hold}
	e, rec := laneExecutor(t, probe, 0, stepObjects(0)...)

	step := stepOperation(0)
	submit(t, e, step)
	waitFor(t, "the step to reach the harness", func() bool {
		_, _, active := probe.seen()
		return active == 1
	})

	if !e.Cancel(step.OperationID) {
		t.Fatal("a running benchmark was not cancellable")
	}
	waitFor(t, "the cancelled step to conclude", func() bool {
		_, ok := concluded(rec, step.OperationID)
		return ok
	})

	frame, _ := concluded(rec, step.OperationID)
	if frame.Failure == nil || frame.Failure.Code != "cancelled" || frame.Failure.Retryable {
		t.Errorf("failure = %+v, want a cancellation nobody should retry", frame.Failure)
	}
}

// TestCloseIsSafeOnAnExecutorThatNeverStartedALane. Several tests in this package build an
// Executor directly to exercise one method; a Close that blocked on a lane that was never made
// would hang the suite rather than fail it.
func TestCloseIsSafeOnAnExecutorThatNeverStartedALane(t *testing.T) {
	done := make(chan struct{})
	go func() {
		defer close(done)
		(&Executor{}).Close()
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Close blocked on a lane that was never started")
	}
}

// withdraw sends an operation.cancel the way the hub does, through the tee's dispatch.
func withdraw(t *testing.T, e *Executor, id string) {
	t.Helper()
	payload, err := json.Marshal(wire.OperationCancel{
		OperationID: id,
		ClusterID:   testCluster,
		Reason:      "superseded by a longer ramp",
		RequestedBy: "operator@example.com",
		RequestedAt: time.Now().UTC().Format(time.RFC3339Nano),
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	handler := WithExecutor(refusingHandler{t: t}, e)
	if err := handler.Handle(context.Background(), &syncpkg.Envelope{
		Type: TypeOperationCancel, Payload: payload,
	}); err != nil {
		t.Fatalf("handle cancel: %v", err)
	}
}

// refusingHandler fails the test if a frame reaches the inner handler.
type refusingHandler struct{ t *testing.T }

func (h refusingHandler) Handle(_ context.Context, e *syncpkg.Envelope) error {
	h.t.Errorf("a %s frame was passed on to the desired-state handler", e.Type)
	return nil
}

func (h refusingHandler) Resync(context.Context) error { return nil }

func TestACancellationFromTheHubStopsAQueuedStep(t *testing.T) {
	hold := make(chan struct{})
	probe := &laneProbe{hold: hold}
	objects := append(stepObjects(0), stepObjects(1)...)
	e, rec := laneExecutor(t, probe, 0, objects...)

	submit(t, e, stepOperation(0))
	waitFor(t, "the first step to reach the harness", func() bool {
		_, _, active := probe.seen()
		return active == 1
	})
	queuedStep := stepOperation(1)
	submit(t, e, queuedStep)

	withdraw(t, e, queuedStep.OperationID)
	close(hold)

	waitFor(t, "the withdrawn step to conclude", func() bool {
		_, ok := concluded(rec, queuedStep.OperationID)
		return ok
	})
	frame, _ := concluded(rec, queuedStep.OperationID)
	if frame.Phase != "failed" || frame.Failure == nil || frame.Failure.Code != "cancelled" {
		t.Fatalf("frame = %+v, want a failure that says it was cancelled", frame)
	}
	if benchmarked(rec, stepBenchmarkID(1)) {
		t.Error("a step the hub withdrew still ran the harness")
	}
}

// TestACancellationForSomethingAlreadyOverIsNotAnError covers the ordinary race.
//
// The operation finishes a moment before the withdrawal lands, which is the common case rather
// than an edge one: a cancellation is a round trip and the work does not pause for it. Reporting a
// phase here would overwrite the conclusion that actually happened with a guess about it.
func TestACancellationForSomethingAlreadyOverIsNotAnError(t *testing.T) {
	probe := &laneProbe{}
	e, rec := laneExecutor(t, probe, 0, stepObjects(0)...)

	step := stepOperation(0)
	submit(t, e, step)
	waitFor(t, "the step to conclude", func() bool {
		_, ok := concluded(rec, step.OperationID)
		return ok
	})
	before, _ := concluded(rec, step.OperationID)

	withdraw(t, e, step.OperationID)

	after, ok := concluded(rec, step.OperationID)
	if !ok || after.Phase != before.Phase {
		t.Fatalf("phase = %q, want the conclusion it already had (%q)", after.Phase, before.Phase)
	}
}

func TestACancellationIsNeverPassedToTheDesiredStateHandler(t *testing.T) {
	probe := &laneProbe{}
	e, _ := laneExecutor(t, probe, 0)

	// refusingHandler fails the test if the frame reaches it.
	withdraw(t, e, "opr_nothing_here_holds_this")
}

// A concluded operation that arrives again is answered with its result, not performed twice.
func TestAReplayOfAConcludedOperationIsAnsweredNotRerun(t *testing.T) {
	op := operation("cordon", "gpu-0", nil)
	rec, e := runOperation(t, op, node("gpu-0", false))
	if rec.last().Phase != "succeeded" {
		t.Fatalf("phase = %q, want succeeded", rec.last().Phase)
	}

	// Somebody uncordons the node by hand; a replay must not undo that.
	var n corev1.Node
	if err := e.Client.Get(context.Background(), types.NamespacedName{Name: "gpu-0"}, &n); err != nil {
		t.Fatal(err)
	}
	n.Spec.Unschedulable = false
	if err := e.Client.Update(context.Background(), &n); err != nil {
		t.Fatal(err)
	}
	before := len(rec.phases())

	payload, _ := json.Marshal(op)
	if err := e.Handle(context.Background(), &syncpkg.Envelope{Type: TypeOperationDesired, Payload: payload}); err != nil {
		t.Fatal(err)
	}
	phases := rec.phases()
	if len(phases) != before+1 || phases[len(phases)-1] != "succeeded" {
		t.Fatalf("phases after replay = %v, want exactly the recorded conclusion again", phases[before:])
	}
	if err := e.Client.Get(context.Background(), types.NamespacedName{Name: "gpu-0"}, &n); err != nil {
		t.Fatal(err)
	}
	if n.Spec.Unschedulable {
		t.Error("the replay cordoned the node again")
	}
}

// The remembered set is bounded, and a retryable refusal is not remembered.
func TestTheCompletedSetIsBoundedAndSkipsRetryableRefusals(t *testing.T) {
	e := New(&Executor{Client: fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).Build(), Log: discardLogger()})
	t.Cleanup(e.Close)
	for i := 0; i < maxCompleted+10; i++ {
		e.remember(fmt.Sprintf("opr_%d", i), wire.OperationObserved{Phase: "succeeded"})
	}
	if _, ok := e.concluded("opr_0"); ok {
		t.Error("the oldest conclusion was kept past the bound")
	}
	if _, ok := e.concluded(fmt.Sprintf("opr_%d", maxCompleted+9)); !ok {
		t.Error("the newest conclusion was not kept")
	}
	e.remember("opr_full", wire.OperationObserved{Phase: "failed", Failure: &wire.OperationFailure{Code: "queue_full", Retryable: true}})
	if _, ok := e.concluded("opr_full"); ok {
		t.Error("a retryable refusal was remembered, so the retry would be answered with the refusal")
	}
}
