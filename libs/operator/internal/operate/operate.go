// Package operate executes the imperative half of the contract.
//
// Operations are the third kind of thing in this design and the one that does not fit the other
// two. Declared state is converged toward forever and observed state is reported forever, but a
// drain starts, takes minutes, and then is over — succeeded or failed. It is never a status field
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
	stdsync "sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/observe"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// TypeOperationDesired and TypeOperationObserved are the two frames this package deals in.
const (
	TypeOperationDesired  = "operation.desired"
	TypeOperationObserved = "operation.observed"
)

// Reporter is the half of the sync client this package needs.
type Reporter interface {
	Send(t syncpkg.MessageType, subject string, payload any) error
}

// Executor accepts operations off the stream and runs them.
//
// Its client is deliberately the direct one rather than the manager's cache. A drain has to see
// every pod on a node, in whatever namespace it happens to live, and the cache is scoped to the
// two namespaces this operator otherwise deals in — an unseen pod would be reported as a node
// successfully emptied.
type Executor struct {
	Client    client.Client
	ClusterID string
	Log       *slog.Logger

	mu       stdsync.Mutex
	reporter Reporter
	running  map[string]struct{}
}

// New fills in an Executor's defaults.
func New(e *Executor) *Executor {
	if e.Log == nil {
		e.Log = slog.Default()
	}
	e.Log = e.Log.With("component", "operate")
	e.running = map[string]struct{}{}
	return e
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
// The work runs on its own goroutine and the frame is acknowledged now. Holding the
// acknowledgement for the length of a drain would stall every other frame behind it on a stream
// that is strictly ordered — desired state for the whole cluster would queue behind one node's
// maintenance window. Progress is reported as it happens instead, which is what
// `operation.observed` is for.
func (e *Executor) Handle(ctx context.Context, env *syncpkg.Envelope) error {
	if env.Type != TypeOperationDesired {
		return nil
	}
	var op wire.OperationDesired
	if err := json.Unmarshal(env.Payload, &op); err != nil {
		return fmt.Errorf("decode %s: %w", env.Type, err)
	}
	if !e.claim(op.OperationID) {
		e.Log.Info("operation already running; ignoring the replay", "operation", op.OperationID)
		return nil
	}
	go e.run(context.WithoutCancel(ctx), op)
	return nil
}

// claim reports whether this operation is ours to start.
//
// The stream is at-least-once, so the same operation arrives again after any resume. Running a
// drain twice concurrently is not idempotent in the way re-applying declared state is — the second
// pass would report progress against evictions the first is still making — so a replay of
// something already in flight is dropped rather than started.
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
	defer e.mu.Unlock()
	delete(e.running, id)
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
// An unsupported kind fails loudly rather than being ignored. Silence would leave the operation
// pending until it timed out, and a timeout says the cluster was slow when what happened is that
// it was asked for something it cannot do.
func (e *Executor) dispatch(ctx context.Context, op wire.OperationDesired) (map[string]string, error) {
	switch op.Kind {
	case "cordon":
		return e.cordon(ctx, op)
	case "drain":
		return e.drain(ctx, op)
	default:
		return nil, fmt.Errorf("this operator cannot perform %q", op.Kind)
	}
}

// cordon marks a node schedulable or not.
//
// One wire kind covers both directions because they are one mechanism, and the parameter says
// which way. A separate `uncordon` kind would double the vocabulary to express a boolean.
func (e *Executor) cordon(ctx context.Context, op wire.OperationDesired) (map[string]string, error) {
	node, err := e.node(ctx, op.Target)
	if err != nil {
		return nil, err
	}
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

// drain empties a node of the workloads this plane placed on it.
//
// Eviction rather than deletion, because eviction is the request the API server can refuse: a pod
// covered by a disruption budget that is already at its floor stays put, and the drain waits
// rather than taking the last replica of something down. Deleting would succeed every time and be
// exactly the outage this operation exists to avoid causing.
//
// Only pods holding accelerators are evicted. A drain here is a request to stop serving on a node,
// not to empty it entirely — the agents, exporters and CSI drivers that make the node reportable
// are the last things that should go, and taking them out would blind the plane to the drain it
// just asked for.
func (e *Executor) drain(ctx context.Context, op wire.OperationDesired) (map[string]string, error) {
	node, err := e.node(ctx, op.Target)
	if err != nil {
		return nil, err
	}

	if _, err := e.cordon(ctx, cordoning(op)); err != nil {
		return nil, err
	}

	var pods corev1.PodList
	selector := client.MatchingFieldsSelector{Selector: fields.OneTermEqualSelector("spec.nodeName", node.Name)}
	if err := e.Client.List(ctx, &pods, selector); err != nil {
		return nil, fmt.Errorf("list pods on %s: %w", node.Name, err)
	}

	targets := make([]*corev1.Pod, 0, len(pods.Items))
	for i := range pods.Items {
		if holdsAccelerator(&pods.Items[i]) {
			targets = append(targets, &pods.Items[i])
		}
	}

	grace := intParam(op.Parameters, "grace_period_seconds", 0)
	evicted, remaining := 0, 0
	for _, pod := range targets {
		if err := e.evict(ctx, pod, grace); err != nil {
			remaining++
			e.Log.Warn("eviction refused", "pod", pod.Name, "node", node.Name, "error", err)
			continue
		}
		evicted++
		e.report(op, running(op, time.Now().UTC(),
			float64(evicted)/float64(len(targets)),
			fmt.Sprintf("evicted %d of %d replicas", evicted, len(targets))))
	}

	result := map[string]string{
		"node":                node.Name,
		"evicted_replicas":    strconv.Itoa(evicted),
		"remaining_replicas":  strconv.Itoa(remaining),
		"accelerator_pods":    strconv.Itoa(len(targets)),
		"grace_period_second": strconv.Itoa(grace),
	}
	if remaining > 0 {
		return result, fmt.Errorf("%d of %d pods could not be evicted", remaining, len(targets))
	}
	return result, nil
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
// the id for every node and look for the match. Deriving rather than storing is the whole point of
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

// holdsAccelerator reports whether a pod is one this plane's workloads would be using devices for.
func holdsAccelerator(pod *corev1.Pod) bool {
	if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
		return false
	}
	for _, container := range pod.Spec.Containers {
		if q, ok := container.Resources.Limits["nvidia.com/gpu"]; ok && !q.IsZero() {
			return true
		}
	}
	return false
}

// cordoning is the drain's first step expressed as the operation it already is.
func cordoning(op wire.OperationDesired) wire.OperationDesired {
	out := op
	out.Parameters = map[string]string{"cordon": "true"}
	return out
}

func (e *Executor) report(op wire.OperationDesired, payload wire.OperationObserved) {
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
// disruption budget should be retried is a decision about the fleet — another cluster may have
// taken the load by now, or the maintenance window may have closed — and that decision belongs
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
