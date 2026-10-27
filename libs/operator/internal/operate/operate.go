// Package operate executes the imperative half of the contract.
//
// Operations are the third kind of thing in this design and the one that does not fit the other
// two. Declared state is converged toward forever and observed state is reported forever, but a
// drain starts, takes minutes, and then is over: succeeded or failed. It is never a status field
// on the object it acted upon, because the object outlives it.
//
// That shape drives everything here. An operation is not stored as desired state and is never
// pruned against a resync snapshot: a snapshot that omitted a running drain would cancel work
// already half-done. It is accepted, run to a conclusion, and reported.
package operate

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	stdsync "sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/observe"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// TypeOperationDesired and TypeOperationObserved are the two frames this package deals in.
const (
	TypeOperationDesired  = "operation.desired"
	TypeOperationObserved = "operation.observed"
	TypeOperationCancel   = "operation.cancel"
)

// The operation kinds this executor performs. The plane spells a benchmark `deployment.benchmark`
// in its own vocabulary and puts `benchmark` on the wire; this is that wire kind.
const (
	KindCordon    = "cordon"
	KindDrain     = "drain"
	KindBenchmark = "benchmark"
)

// defaultBenchmarkQueue is how many benchmark steps may wait for the lane.
//
// A ramp is the case this depth is for: one request becomes a step per concurrency level, all of
// them arriving within a second of each other, and every one of them has to be kept. Deep enough
// for a long ramp, and still bounded: an unbounded queue would turn a plane
// that keeps asking into a cluster that keeps agreeing and never measures anything.
const defaultBenchmarkQueue = 64

// Drain defaults: how long a drain may take in all, and how often it retries a refused eviction
// or checks that an evicted pod has exited. The retry interval doubles up to maxDrainRetry.
const (
	defaultDrainTimeout = 10 * time.Minute
	defaultDrainRetry   = 2 * time.Second
	maxDrainRetry       = 30 * time.Second
)

// maxCompleted bounds how many concluded operations are remembered for answering replays.
const maxCompleted = 1024

// Reporter is the half of the sync client this package needs.
type Reporter interface {
	Send(t syncpkg.MessageType, subject string, payload any) error
}

// Executor accepts operations off the stream and runs them.
//
// Its client is the direct one instead of the manager's cache. A drain has to see
// every pod on a node, in whatever namespace it happens to live, and the cache is scoped to the
// two namespaces this operator otherwise deals in: an unseen pod would be reported as a node
// successfully emptied.
type Executor struct {
	Client    client.Client
	ClusterID string
	Log       *slog.Logger

	// ReadOnly refuses every operation except the kinds Allowed names. See
	// config.Config.ReadOnly and config.Config.AllowedOperations.
	ReadOnly bool

	// Allowed are the operation kinds a read-only operator still performs. Empty means none.
	Allowed []string

	// Render and Benchmark are what a benchmark run needs to become a job: the cluster's local
	// scheduling conventions, and the endpoint the load is aimed at. Both are per-cluster facts
	// the plane does not declare.
	Render    render.Options
	Benchmark render.BenchmarkOptions

	// Logs reads a finished job's output back, which is how a harness's result gets here.
	// Nil leaves benchmarks refusing instead of running blind.
	Logs LogReader

	// BenchmarkTimeout bounds a run whose operation carried no deadline of its own.
	BenchmarkTimeout time.Duration

	// BenchmarkQueue is how many benchmark steps may wait for the lane. Zero takes
	// defaultBenchmarkQueue.
	BenchmarkQueue int

	// DrainTimeout bounds a drain that carries no deadline of its own, and DrainRetry is the
	// first interval between retries of a refused eviction. Zero takes the defaults.
	DrainTimeout time.Duration
	DrainRetry   time.Duration

	mu       stdsync.Mutex
	reporter Reporter
	running  map[string]struct{}
	cancels  map[string]context.CancelFunc

	// completed holds the terminal frame of each concluded operation, oldest first in
	// completedOrder, so a replay is answered with the result, not run again.
	completed      map[string]wire.OperationObserved
	completedOrder []string

	lane      chan queued
	laneCtx   context.Context
	laneStop  context.CancelFunc
	laneDone  chan struct{}
	closeOnce stdsync.Once
}

// queued is a benchmark waiting for the lane, with the context that cancels it.
type queued struct {
	ctx context.Context
	op  wire.OperationDesired
}

// New fills in an Executor's defaults and starts the benchmark lane.
//
// The lane's worker is started here, not by a caller, because an executor that accepted a
// benchmark with nothing draining the lane would hold the step for ever and report nothing. It
// stops on Close, or when the context handed to Start is cancelled.
func New(e *Executor) *Executor {
	if e.Log == nil {
		e.Log = slog.Default()
	}
	e.Log = e.Log.With("component", "operate")
	e.running = map[string]struct{}{}
	e.cancels = map[string]context.CancelFunc{}
	e.completed = map[string]wire.OperationObserved{}
	depth := e.BenchmarkQueue
	if depth <= 0 {
		depth = defaultBenchmarkQueue
	}
	e.lane = make(chan queued, depth)
	e.laneCtx, e.laneStop = context.WithCancel(context.Background())
	e.laneDone = make(chan struct{})
	go e.serve()
	return e
}

// Start ties the benchmark lane's lifetime to ctx.
//
// Optional: an executor whose owner never calls it runs until Close. It exists so that a process
// shutting down through its root context takes the lane with it instead of leaving the worker
// behind.
func (e *Executor) Start(ctx context.Context) {
	go func() {
		select {
		case <-ctx.Done():
			e.Close()
		case <-e.laneDone:
		}
	}()
}

// Close stops the benchmark lane and waits for the step in flight.
//
// Waiting instead of returning at once, because the step is a job in the cluster and the frames
// that say how it ended are written by the goroutine being stopped. It is safe to call twice, and
// on an executor that never went through New there is no lane to stop.
func (e *Executor) Close() {
	if e.laneDone == nil {
		return
	}
	e.closeOnce.Do(e.laneStop)
	<-e.laneDone
}

// laneClosing reports whether the lane is on its way out, which is how a run that was cut short
// tells being stopped from being cancelled.
func (e *Executor) laneClosing() bool {
	return e.laneCtx != nil && e.laneCtx.Err() != nil
}

// serve runs queued benchmarks one at a time, in the order they arrived.
//
// One worker and no more is the point. A ramp is a sequence of steps at rising concurrency,
// and steps that overlap measure each other: every point on the curve carries the load of every
// other point, so the knee the ramp exists to find is not in the numbers at all. Every other kind
// keeps its goroutine per operation: a drain waiting behind a twenty-minute benchmark would be a
// maintenance window missed for a measurement.
func (e *Executor) serve() {
	defer close(e.laneDone)
	for {
		if e.laneCtx.Err() != nil {
			e.abandon()
			return
		}
		select {
		case <-e.laneCtx.Done():
			e.abandon()
			return
		case work := <-e.lane:
			e.serveOne(work)
		}
	}
}

// abandon refuses every step still waiting when the lane stops.
//
// Silence here would leave the plane holding a ramp whose last four steps never happened and never
// said so. A refusal that is retryable is the truth: this operator is going away, and the steps can
// be asked for again of whoever comes back.
func (e *Executor) abandon() {
	for {
		select {
		case work := <-e.lane:
			e.serveOne(work)
		default:
			return
		}
	}
}

// serveOne runs one queued benchmark, unless it was cancelled while it waited.
//
// A step whose context went with the lane's was not cancelled by anybody, the operator is
// stopping, and saying so is the difference between a step that can be asked for again and one
// somebody has to work out was never really refused.
func (e *Executor) serveOne(work queued) {
	if work.ctx.Err() == nil {
		e.run(work.ctx, work.op)
		return
	}
	if e.laneCtx.Err() != nil {
		e.Log.Warn("the benchmark lane stopped with a step still waiting",
			"operation", work.op.OperationID)
		e.refuse(work.op, "shutting_down", true,
			fmt.Errorf("this operator stopped before the benchmark could start"))
		return
	}
	e.Log.Info("benchmark cancelled before it started", "operation", work.op.OperationID)
	e.refuse(work.op, "cancelled", false,
		fmt.Errorf("the benchmark was cancelled while it waited for the lane"))
}

// Bind supplies the Reporter after construction, for the same reason the observer needs it: the
// sync client and the things it feeds cannot both be built first.
func (e *Executor) Bind(r Reporter) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.reporter = r
}

// Handle accepts an operation and returns immediately.
//
// The work runs elsewhere and the frame is acknowledged now. Holding the acknowledgement for the
// length of a drain would stall every other frame behind it on a stream that is strictly ordered:
// desired state for the whole cluster would queue behind one node's maintenance window. Progress
// is reported as it happens instead, which is what `operation.observed` is for.
//
// Elsewhere is a goroutine of its own for every kind but one. A benchmark goes on a lane that runs
// its steps in turn, because concurrent benchmarks against the same deployment measure each other.
// Enqueuing never blocks the reader: a lane with no room refuses the step out loud instead of
// making the stream wait for a measurement to finish.
func (e *Executor) Handle(ctx context.Context, env *syncpkg.Envelope) error {
	if env.Type != TypeOperationDesired {
		return nil
	}
	var op wire.OperationDesired
	if err := json.Unmarshal(env.Payload, &op); err != nil {
		return fmt.Errorf("decode %s: %w", env.Type, err)
	}
	if frame, done := e.concluded(op.OperationID); done {
		e.Log.Info("operation already concluded; answering the replay with its result", "operation", op.OperationID)
		e.send(op, frame)
		return nil
	}
	if !e.claim(op.OperationID) {
		e.Log.Info("operation already running; ignoring the replay", "operation", op.OperationID)
		return nil
	}
	if op.Kind == KindBenchmark {
		e.enqueue(op)
		return nil
	}
	go e.run(e.track(op.OperationID, context.WithoutCancel(ctx)), op)
	return nil
}

// HandleCancel stops an operation this operator was already told to perform.
//
// It answers nothing on the wire itself. The cancelled operation reports its own terminal phase
// through the path every other conclusion takes, so a cancellation that arrives for an id this
// process does not hold is a log line and not a failure: the operation may have finished a moment
// earlier, or been refused for a full lane, or belonged to an operator that has since restarted.
// Reporting a phase for work this process never had would overwrite whatever actually happened
// with a guess.
func (e *Executor) HandleCancel(ctx context.Context, env *syncpkg.Envelope) error {
	if env.Type != TypeOperationCancel {
		return nil
	}
	var msg wire.OperationCancel
	if err := json.Unmarshal(env.Payload, &msg); err != nil {
		return fmt.Errorf("decode %s: %w", env.Type, err)
	}
	if e.Cancel(msg.OperationID) {
		e.Log.Info("cancelling an operation the plane withdrew",
			"operation", msg.OperationID, "reason", msg.Reason)
		return nil
	}
	e.Log.Info("nothing here to cancel; the operation is already over or was never accepted",
		"operation", msg.OperationID)
	return nil
}

// enqueue puts a benchmark on the lane, or refuses it where it can be seen.
//
// A step that vanished would be worse than one that failed. The plane would go on waiting for a
// point on a curve that nobody is measuring, and the ramp would end short with no reason recorded
// anywhere, so a full lane is a loud log and a failed operation, which is a conclusion the plane
// already knows how to read.
func (e *Executor) enqueue(op wire.OperationDesired) {
	ctx := e.track(op.OperationID, e.laneCtx)
	if err := e.laneCtx.Err(); err == nil {
		select {
		case e.lane <- queued{ctx: ctx, op: op}:
			e.Log.Info("benchmark queued", "operation", op.OperationID, "waiting", len(e.lane))
			// The lane can stop between the check above and this send, which would leave
			// the step in a queue nobody drains again. Sweeping here instead of locking
			// the two together: refusing a step twice is impossible, and the sweep is a
			// no-op in every case but that one.
			if e.laneCtx.Err() != nil {
				e.abandon()
			}
			return
		default:
		}
		e.Log.Error("the benchmark lane is full; refusing this step",
			"operation", op.OperationID, "depth", cap(e.lane))
		e.refuse(op, "queue_full", true, fmt.Errorf(
			"this operator already holds %d benchmarks waiting to run", cap(e.lane)))
		return
	}
	e.Log.Warn("the benchmark lane is closed; refusing this step", "operation", op.OperationID)
	e.refuse(op, "shutting_down", true,
		fmt.Errorf("this operator is shutting down and will not start another benchmark"))
}

func (e *Executor) refuse(op wire.OperationDesired, code string, retryable bool, err error) {
	e.report(op, refused(op, time.Now().UTC(), code, retryable, err))
	e.release(op.OperationID)
}

// Cancel stops an operation this executor accepted, whether it is running or still queued.
//
// Both, and not only the running one, because a ramp puts most of its steps in a queue: a
// cancellation that reached only the step in flight would let the five behind it run anyway,
// against a deployment somebody has already decided to stop measuring.
//
// It reports whether the operation was here to cancel, which is what lets HandleCancel tell a
// cancellation that landed from one that arrived after the work was already over.
func (e *Executor) Cancel(id string) bool {
	e.mu.Lock()
	cancel, ok := e.cancels[id]
	e.mu.Unlock()
	if ok {
		cancel()
	}
	return ok
}

// track derives the context an operation runs under and keeps its cancel where Cancel can find it.
func (e *Executor) track(id string, parent context.Context) context.Context {
	ctx, cancel := context.WithCancel(parent)
	e.mu.Lock()
	defer e.mu.Unlock()
	e.cancels[id] = cancel
	return ctx
}

// concluded returns the terminal frame of an operation this executor already finished.
func (e *Executor) concluded(id string) (wire.OperationObserved, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	frame, ok := e.completed[id]
	return frame, ok
}

// remember records a conclusion so a replay of the operation is answered, not re-run.
//
// A retryable refusal is not remembered: it says "not now", and the plane asking again under the
// same id is how it retries. The set is bounded, oldest first out, and lives only as long as the
// process; nodeOperationsAnnotation and a benchmark's Job carry the record across a restart.
func (e *Executor) remember(id string, frame wire.OperationObserved) {
	if frame.Phase != "succeeded" && frame.Phase != "failed" {
		return
	}
	if frame.Failure != nil && frame.Failure.Retryable {
		return
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.completed == nil {
		e.completed = map[string]wire.OperationObserved{}
	}
	if _, seen := e.completed[id]; !seen {
		e.completedOrder = append(e.completedOrder, id)
	}
	e.completed[id] = frame
	for len(e.completedOrder) > maxCompleted {
		delete(e.completed, e.completedOrder[0])
		e.completedOrder = e.completedOrder[1:]
	}
}

// claim reports whether this operation is ours to start.
//
// The stream is at-least-once, so the same operation arrives again after any resume. Running a
// drain twice concurrently is not idempotent in the way re-applying declared state is, the second
// pass would report progress against evictions the first is still making, so a replay of
// something already in flight is dropped, not started.
func (e *Executor) claim(id string) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, busy := e.running[id]; busy {
		return false
	}
	e.running[id] = struct{}{}
	return true
}

func (e *Executor) release(id string) {
	e.mu.Lock()
	cancel := e.cancels[id]
	delete(e.cancels, id)
	delete(e.running, id)
	e.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (e *Executor) run(ctx context.Context, op wire.OperationDesired) {
	defer e.release(op.OperationID)
	started := time.Now().UTC()
	e.report(op, running(op, started, 0, "started"))

	if deadline, ok := parseTime(op.Deadline); ok {
		var cancel context.CancelFunc
		ctx, cancel = context.WithDeadline(ctx, deadline)
		defer cancel()
	}

	result, err := e.dispatch(ctx, op)
	completed := time.Now().UTC()
	switch {
	case err != nil && ctx.Err() == context.Canceled:
		// The work did not fail; it was stopped. Reported as an ordinary execution failure it
		// would read as a measurement this cluster cannot make, and a ramp step nobody asks
		// for again, so it carries the reason it was stopped for, and whether asking again
		// is worth anything.
		code, retryable := "cancelled", false
		reason := fmt.Errorf("the operation was cancelled while it ran")
		if e.laneClosing() {
			code, retryable = "shutting_down", true
			reason = fmt.Errorf("this operator stopped while the operation was running")
		}
		e.Log.Warn("operation stopped before it concluded",
			"operation", op.OperationID, "kind", op.Kind, "code", code, "error", err)
		e.report(op, stopped(op, started, completed, result, code, retryable, reason))
	case err != nil:
		e.Log.Error("operation failed", "operation", op.OperationID, "kind", op.Kind, "error", err)
		e.report(op, failed(op, started, completed, result, err))
	default:
		e.Log.Info("operation succeeded", "operation", op.OperationID, "kind", op.Kind)
		e.report(op, succeeded(op, started, completed, result))
	}
}

// dispatch routes an operation to the thing that performs it.
//
// An unsupported kind fails loudly instead of being ignored. Silence would leave the operation
// pending until it timed out, and a timeout says the cluster was slow when what happened is that
// it was asked for something it cannot do.
//
// A refused kind is refused here, not at the handler, so that the refusal travels the same
// route as any other failure: an `operation.observed` that says why.
func (e *Executor) dispatch(ctx context.Context, op wire.OperationDesired) (map[string]string, error) {
	if e.ReadOnly && !e.permits(op.Kind) {
		if len(e.Allowed) == 0 {
			return nil, fmt.Errorf("this operator is read-only and will not perform %q", op.Kind)
		}
		return nil, fmt.Errorf(
			"this operator is read-only and performs only %s, not %q",
			strings.Join(e.Allowed, ", "), op.Kind,
		)
	}
	switch op.Kind {
	case KindCordon, KindDrain:
		return e.nodeOperation(ctx, op)
	case KindBenchmark:
		return e.benchmark(ctx, op)
	default:
		return nil, fmt.Errorf("this operator cannot perform %q", op.Kind)
	}
}

// permits reports whether a read-only operator still performs this kind.
func (e *Executor) permits(kind string) bool {
	for _, allowed := range e.Allowed {
		if allowed == kind {
			return true
		}
	}
	return false
}

// nodeOperationsAnnotation records the last few node operations concluded on a node, as
// space-separated id=phase pairs, oldest first.
//
// The executor's in-memory set answers a replay while the process lives. This one survives a
// restart or a leader change, which is when a replay is most likely: the plane never received
// the terminal frame, so it sends the operation again to an operator that has no memory of it.
// Kept on the node because a drain already needs to patch it, so the record costs no extra
// permission; a benchmark's record is its Job, which a replay re-attaches to by name.
const nodeOperationsAnnotation = "hangar.dev/operations"

// maxNodeOperations bounds the record on one node.
const maxNodeOperations = 8

// nodeOperation resolves the node an operation targets, answers a replay from the record on the
// node, performs the operation, and records its conclusion there.
func (e *Executor) nodeOperation(ctx context.Context, op wire.OperationDesired) (map[string]string, error) {
	node, err := e.node(ctx, op.Target)
	if err != nil {
		return nil, err
	}
	if err := e.inGPUPool(node, op); err != nil {
		return nil, err
	}
	if phase, ok := recordedOperation(node, op.OperationID); ok {
		e.Log.Info("operation already concluded on this node; answering the replay", "operation", op.OperationID, "node", node.Name, "phase", phase)
		result := map[string]string{"node": node.Name, "replayed": "true"}
		if phase != "succeeded" {
			return result, fmt.Errorf("operation %s already %s on %s; send a new operation to try again", op.OperationID, phase, node.Name)
		}
		return result, nil
	}

	var result map[string]string
	if op.Kind == KindDrain {
		result, err = e.drain(ctx, op, node)
	} else {
		result, err = e.cordon(ctx, op, node)
	}
	// A cancelled operation is not a conclusion the plane will ask about again under this id.
	if ctx.Err() != context.Canceled {
		phase := "succeeded"
		if err != nil {
			phase = "failed"
		}
		e.recordOperation(node.Name, op.OperationID, phase)
	}
	return result, err
}

// inGPUPool refuses a node outside the accelerator pool unless the operation sets force=true.
// Hangar places work only on that pool, and a wrong node id should not be enough to cordon or
// drain a system node.
func (e *Executor) inGPUPool(node *corev1.Node, op wire.OperationDesired) error {
	key, value := e.Render.PoolLabelKey, e.Render.GPUPoolValue
	if key == "" || value == "" || boolParam(op.Parameters, "force", false) {
		return nil
	}
	if node.Labels[key] == value {
		return nil
	}
	return fmt.Errorf("node %s is not in the %s=%s pool; set force=true to %s it anyway", node.Name, key, value, op.Kind)
}

// recordedOperation returns the phase the node's record holds for an operation.
func recordedOperation(node *corev1.Node, id string) (string, bool) {
	for _, entry := range strings.Fields(node.Annotations[nodeOperationsAnnotation]) {
		if recorded, phase, ok := strings.Cut(entry, "="); ok && recorded == id {
			return phase, true
		}
	}
	return "", false
}

// recordOperation appends a conclusion to the node's record, dropping the oldest past the bound.
// A failure to record is logged, not reported: the operation itself is already over.
func (e *Executor) recordOperation(nodeName, id, phase string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var node corev1.Node
	if err := e.Client.Get(ctx, client.ObjectKey{Name: nodeName}, &node); err != nil {
		e.Log.Warn("could not record the operation on its node", "operation", id, "node", nodeName, "error", err)
		return
	}
	entries := strings.Fields(node.Annotations[nodeOperationsAnnotation])
	entries = append(entries, id+"="+phase)
	if len(entries) > maxNodeOperations {
		entries = entries[len(entries)-maxNodeOperations:]
	}
	patch := client.MergeFrom(node.DeepCopy())
	if node.Annotations == nil {
		node.Annotations = map[string]string{}
	}
	node.Annotations[nodeOperationsAnnotation] = strings.Join(entries, " ")
	if err := e.Client.Patch(ctx, &node, patch); err != nil {
		e.Log.Warn("could not record the operation on its node", "operation", id, "node", nodeName, "error", err)
	}
}

// cordon marks a node schedulable or not.
//
// One wire kind covers both directions because they are one mechanism, and the parameter says
// which way. A separate `uncordon` kind would double the vocabulary to express a boolean.
func (e *Executor) cordon(ctx context.Context, op wire.OperationDesired, node *corev1.Node) (map[string]string, error) {
	want := boolParam(op.Parameters, "cordon", true)
	if node.Spec.Unschedulable == want {
		return map[string]string{"node": node.Name, "changed": "false"}, nil
	}
	patch := client.MergeFrom(node.DeepCopy())
	node.Spec.Unschedulable = want
	if err := e.Client.Patch(ctx, node, patch); err != nil {
		return nil, fmt.Errorf("cordon %s: %w", node.Name, err)
	}
	return map[string]string{"node": node.Name, "changed": "true",
		"unschedulable": strconv.FormatBool(want)}, nil
}

// drain empties a node of the accelerator workloads this operator placed on it.
//
// Eviction, not deletion, because eviction is the request the API server can refuse: a pod
// covered by a disruption budget at its floor stays put. A refused eviction is retried with
// backoff until the drain's deadline, and the drain then waits for every evicted pod to exit, so
// success means the node is actually empty of them.
//
// Only Hangar-managed pods holding accelerators are evicted. Agents, exporters and CSI drivers stay
// so the plane can still see the node, and accelerator pods this operator did not place stay unless
// the operation sets `force=true`; how many were left is in the result.
func (e *Executor) drain(ctx context.Context, op wire.OperationDesired, node *corev1.Node) (map[string]string, error) {
	if _, err := e.cordon(ctx, cordoning(op), node); err != nil {
		return nil, err
	}

	var pods corev1.PodList
	selector := client.MatchingFieldsSelector{Selector: fields.OneTermEqualSelector("spec.nodeName", node.Name)}
	if err := e.Client.List(ctx, &pods, selector); err != nil {
		return nil, fmt.Errorf("list pods on %s: %w", node.Name, err)
	}

	force := boolParam(op.Parameters, "force", false)
	targets := make([]*corev1.Pod, 0, len(pods.Items))
	unmanaged := 0
	for i := range pods.Items {
		pod := &pods.Items[i]
		if !holdsAccelerator(pod) {
			continue
		}
		if !force && !managedByHangar(pod) {
			unmanaged++
			continue
		}
		targets = append(targets, pod)
	}

	timeout := e.DrainTimeout
	if timeout <= 0 {
		timeout = defaultDrainTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	grace := intParam(op.Parameters, "grace_period_seconds", 0)
	evicted := make([]*corev1.Pod, 0, len(targets))
	pending := targets
	retry := e.drainRetry()
	for len(pending) > 0 {
		var refusedPods []*corev1.Pod
		for _, pod := range pending {
			err := e.evict(ctx, pod, grace)
			switch {
			case err == nil, apierrors.IsNotFound(err):
				evicted = append(evicted, pod)
				e.report(op, running(op, time.Now().UTC(),
					float64(len(evicted))/float64(len(targets)),
					fmt.Sprintf("evicted %d of %d replicas", len(evicted), len(targets))))
			case apierrors.IsTooManyRequests(err):
				refusedPods = append(refusedPods, pod)
				e.Log.Info("eviction refused by a disruption budget; will retry", "pod", pod.Name, "node", node.Name)
			default:
				return drainResult(node, targets, evicted, len(pending), unmanaged, grace, 0),
					fmt.Errorf("evict %s/%s: %w", pod.Namespace, pod.Name, err)
			}
		}
		pending = refusedPods
		if len(pending) == 0 || !sleep(ctx, retry) {
			break
		}
		retry = min(retry*2, maxDrainRetry)
	}

	exiting := e.awaitExit(ctx, evicted)
	result := drainResult(node, targets, evicted, len(pending), unmanaged, grace, exiting)
	if len(pending) > 0 {
		return result, fmt.Errorf("%d of %d pods could not be evicted before the drain's deadline", len(pending), len(targets))
	}
	if exiting > 0 {
		return result, fmt.Errorf("%d evicted pods had not exited before the drain's deadline", exiting)
	}
	return result, nil
}

func drainResult(node *corev1.Node, targets, evicted []*corev1.Pod, remaining, unmanaged, grace, exiting int) map[string]string {
	return map[string]string{
		"node":                       node.Name,
		"evicted_replicas":           strconv.Itoa(len(evicted)),
		"remaining_replicas":         strconv.Itoa(remaining),
		"exiting_replicas":           strconv.Itoa(exiting),
		"accelerator_pods":           strconv.Itoa(len(targets)),
		"unmanaged_accelerator_pods": strconv.Itoa(unmanaged),
		"grace_period_second":        strconv.Itoa(grace),
	}
}

// awaitExit waits until every evicted pod is gone, or replaced by a new pod of the same name, and
// returns how many were still there when ctx ended.
func (e *Executor) awaitExit(ctx context.Context, evicted []*corev1.Pod) int {
	waiting := evicted
	poll := e.drainRetry()
	for {
		var still []*corev1.Pod
		for _, pod := range waiting {
			var current corev1.Pod
			err := e.Client.Get(ctx, client.ObjectKeyFromObject(pod), &current)
			if apierrors.IsNotFound(err) || (err == nil && current.UID != pod.UID) {
				continue
			}
			still = append(still, pod)
		}
		if len(still) == 0 {
			return 0
		}
		waiting = still
		if !sleep(ctx, poll) {
			return len(waiting)
		}
		poll = min(poll*2, maxDrainRetry)
	}
}

func (e *Executor) drainRetry() time.Duration {
	if e.DrainRetry > 0 {
		return e.DrainRetry
	}
	return defaultDrainRetry
}

// sleep waits for d, and reports false if ctx ended first.
func sleep(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func (e *Executor) evict(ctx context.Context, pod *corev1.Pod, grace int) error {
	eviction := &policyv1.Eviction{ObjectMeta: metav1.ObjectMeta{Namespace: pod.Namespace, Name: pod.Name}}
	if grace > 0 {
		seconds := int64(grace)
		eviction.DeleteOptions = &metav1.DeleteOptions{GracePeriodSeconds: &seconds}
	}
	return e.Client.SubResource("eviction").Create(ctx, pod, eviction)
}

// node resolves an operation's target to the node it names.
//
// Observed ids are one-way derivations of a natural key (ADR 0002), so the way back is to re-mint
// the id for every node and look for the match. Deriving instead of storing is the point of
// that decision: there is no side table to disagree with the cluster, and a restarted operator
// resolves the same id to the same node.
func (e *Executor) node(ctx context.Context, target wire.OperationTarget) (*corev1.Node, error) {
	if target.Kind != "node" {
		return nil, fmt.Errorf("expected a node target, got %q", target.Kind)
	}
	var nodes corev1.NodeList
	if err := e.Client.List(ctx, &nodes); err != nil {
		return nil, fmt.Errorf("list nodes: %w", err)
	}
	for i := range nodes.Items {
		if observe.NodeID(e.ClusterID, nodes.Items[i].Name) == target.ID {
			return &nodes.Items[i], nil
		}
	}
	return nil, fmt.Errorf("no node in this cluster has id %s", target.ID)
}

// holdsAccelerator reports whether a running pod holds accelerators.
func holdsAccelerator(pod *corev1.Pod) bool {
	if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
		return false
	}
	for _, container := range pod.Spec.Containers {
		if q, ok := container.Resources.Limits[render.GPUResource]; ok && !q.IsZero() {
			return true
		}
	}
	return false
}

// managedByHangar reports whether a pod was rendered by this operator. The render puts its
// ownership labels on every pod template.
func managedByHangar(pod *corev1.Pod) bool {
	return pod.Labels[render.ManagedByLabel] == managedByOperator
}

// managedByOperator is the value render stamps on render.ManagedByLabel.
const managedByOperator = "hangar-operator"

// cordoning is the drain's first step expressed as the operation it already is.
func cordoning(op wire.OperationDesired) wire.OperationDesired {
	out := op
	out.Parameters = map[string]string{"cordon": "true"}
	return out
}

func (e *Executor) report(op wire.OperationDesired, payload wire.OperationObserved) {
	e.remember(op.OperationID, payload)
	e.send(op, payload)
}

func (e *Executor) send(op wire.OperationDesired, payload wire.OperationObserved) {
	e.mu.Lock()
	reporter := e.reporter
	e.mu.Unlock()
	if reporter == nil {
		e.Log.Warn("no reporter bound; dropping operation status", "operation", op.OperationID)
		return
	}
	if err := reporter.Send(TypeOperationObserved, op.OperationID, payload); err != nil {
		e.Log.Error("could not report operation status", "operation", op.OperationID, "error", err)
	}
}

func running(op wire.OperationDesired, started time.Time, progress float64, message string) wire.OperationObserved {
	return wire.OperationObserved{
		OperationID: op.OperationID,
		ClusterID:   op.ClusterID,
		Phase:       "running",
		Progress:    progress,
		Message:     message,
		StartedAt:   stamp(started),
		ObservedAt:  at(time.Now().UTC()),
	}
}

func succeeded(op wire.OperationDesired, started, completed time.Time, result map[string]string) wire.OperationObserved {
	return wire.OperationObserved{
		OperationID: op.OperationID,
		ClusterID:   op.ClusterID,
		Phase:       "succeeded",
		Progress:    1,
		Result:      result,
		StartedAt:   stamp(started),
		CompletedAt: stamp(completed),
		ObservedAt:  at(completed),
	}
}

// failed reports a conclusion, not a request to try again.
//
// Retryable is false for everything this executor gives up on. Whether a drain that hit a
// disruption budget should be retried is a decision about the fleet, another cluster may have
// taken the load by now, or the maintenance window may have closed, and that decision belongs
// where the fleet is visible, which is not here.
func failed(op wire.OperationDesired, started, completed time.Time, result map[string]string, err error) wire.OperationObserved {
	return wire.OperationObserved{
		OperationID: op.OperationID,
		ClusterID:   op.ClusterID,
		Phase:       "failed",
		Result:      result,
		Failure:     &wire.OperationFailure{Code: "execution", Message: err.Error(), Retryable: false},
		StartedAt:   stamp(started),
		CompletedAt: stamp(completed),
		ObservedAt:  at(completed),
	}
}

// refused is a conclusion the executor reached without performing the operation at all.
//
// Retryable is true where `failed` has it false, and the difference is accurate, not
// convenient: a lane that is full or closing says nothing about whether the benchmark can be run,
// only that this operator cannot take it now. A cancellation is the other case here and is not
// retryable, because somebody asked for it to stop.
func refused(op wire.OperationDesired, when time.Time, code string, retryable bool, err error) wire.OperationObserved {
	return wire.OperationObserved{
		OperationID: op.OperationID,
		ClusterID:   op.ClusterID,
		Phase:       "failed",
		Failure:     &wire.OperationFailure{Code: code, Message: err.Error(), Retryable: retryable},
		CompletedAt: stamp(when),
		ObservedAt:  at(when),
	}
}

// stopped is a conclusion reached because this operator ended the work, not because the work
// ended. It keeps StartedAt and whatever the operation had measured so far, because the run did
// begin: the difference from `refused`, which reports something that never started at all.
func stopped(op wire.OperationDesired, started, completed time.Time, result map[string]string, code string, retryable bool, err error) wire.OperationObserved {
	return wire.OperationObserved{
		OperationID: op.OperationID,
		ClusterID:   op.ClusterID,
		Phase:       "failed",
		Result:      result,
		Failure:     &wire.OperationFailure{Code: code, Message: err.Error(), Retryable: retryable},
		StartedAt:   stamp(started),
		CompletedAt: stamp(completed),
		ObservedAt:  at(completed),
	}
}

func stamp(t time.Time) *string {
	out := t.UTC().Format("2006-01-02T15:04:05.000Z")
	return &out
}

func at(t time.Time) string {
	return t.UTC().Format("2006-01-02T15:04:05.000Z")
}

func parseTime(value string) (time.Time, bool) {
	if value == "" {
		return time.Time{}, false
	}
	t, err := time.Parse(time.RFC3339, value)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

func boolParam(params map[string]string, key string, def bool) bool {
	if raw, ok := params[key]; ok {
		if v, err := strconv.ParseBool(raw); err == nil {
			return v
		}
	}
	return def
}

func intParam(params map[string]string, key string, def int) int {
	if raw, ok := params[key]; ok {
		if v, err := strconv.Atoi(raw); err == nil {
			return v
		}
	}
	return def
}
