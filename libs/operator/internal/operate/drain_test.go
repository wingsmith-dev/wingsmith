package operate

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// drainClient builds a fake client that can answer the two things a drain does: list pods by the
// node they are on, and evict one. The field index is what the real cache provides; without it the
// selector matches nothing and a drain reports every node already empty.
func drainClient(refuse interceptor.Funcs, objects ...client.Object) client.Client {
	return fake.NewClientBuilder().
		WithScheme(clientgoscheme.Scheme).
		WithIndex(&corev1.Pod{}, "spec.nodeName", func(o client.Object) []string {
			return []string{o.(*corev1.Pod).Spec.NodeName}
		}).
		WithInterceptorFuncs(refuse).
		WithObjects(objects...).
		Build()
}

func runDrain(t *testing.T, c client.Client, op wire.OperationDesired) *recorder {
	t.Helper()
	rec := &recorder{}
	e := New(&Executor{Client: c, ClusterID: testCluster, Log: slog.Default(),
		DrainTimeout: 300 * time.Millisecond, DrainRetry: 5 * time.Millisecond})
	e.Bind(rec)

	payload, err := json.Marshal(op)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := e.Handle(context.Background(), &syncpkg.Envelope{Type: TypeOperationDesired, Payload: payload}); err != nil {
		t.Fatalf("handle: %v", err)
	}

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if p := rec.phases(); len(p) > 0 && (p[len(p)-1] == "succeeded" || p[len(p)-1] == "failed") {
			return rec
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("drain did not reach a terminal phase; saw %v", rec.phases())
	return nil
}

func gone(t *testing.T, c client.Client, namespace, name string) bool {
	t.Helper()
	var p corev1.Pod
	err := c.Get(context.Background(), types.NamespacedName{Namespace: namespace, Name: name}, &p)
	if err == nil {
		return false
	}
	if !apierrors.IsNotFound(err) {
		t.Fatalf("read pod %s/%s: %v", namespace, name, err)
	}
	return true
}

// TestDrainEvictsAcceleratorPodsAndCordonsFirst is the whole operation end to end. Cordoning is
// part of the drain and not a separate step the caller has to remember: a node emptied but still
// schedulable refills before the maintenance it was emptied for begins.
func TestDrainEvictsAcceleratorPodsAndCordonsFirst(t *testing.T) {
	c := drainClient(interceptor.Funcs{},
		node("gpu-0", false),
		acceleratorPod("qwen-worker-0", "gpu-0"),
		acceleratorPod("qwen-worker-1", "gpu-0"),
		plainPod("csi-driver", "gpu-0"),
	)
	rec := runDrain(t, c, operation("drain", "gpu-0", nil))

	last := rec.last()
	if last.Phase != "succeeded" {
		t.Fatalf("phase = %q (%+v), want succeeded", last.Phase, last.Failure)
	}
	if last.Result["evicted_replicas"] != "2" || last.Result["remaining_replicas"] != "0" {
		t.Errorf("result = %v, want 2 evicted and 0 remaining", last.Result)
	}
	if last.Result["accelerator_pods"] != "2" {
		t.Errorf("accelerator_pods = %q, want 2", last.Result["accelerator_pods"])
	}

	var after corev1.Node
	if err := c.Get(context.Background(), types.NamespacedName{Name: "gpu-0"}, &after); err != nil {
		t.Fatalf("read node: %v", err)
	}
	if !after.Spec.Unschedulable {
		t.Error("the drain left the node schedulable; it will refill before maintenance starts")
	}
	if !gone(t, c, "dynamo", "qwen-worker-0") || !gone(t, c, "dynamo", "qwen-worker-1") {
		t.Error("an accelerator pod survived the drain")
	}
	// The rule the package doc states: a drain stops serving, it does not empty the node.
	if gone(t, c, "kube-system", "csi-driver") {
		t.Error("the drain evicted a non-accelerator pod; the plane is now blind to the node it drained")
	}
}

// TestDrainLeavesPodsOnOtherNodesAlone guards the field selector. Without the index the list is
// unfiltered, and a drain of one node would empty the fleet.
func TestDrainLeavesPodsOnOtherNodesAlone(t *testing.T) {
	c := drainClient(interceptor.Funcs{},
		node("gpu-0", false),
		acceleratorPod("here", "gpu-0"),
		acceleratorPod("elsewhere", "gpu-1"),
	)
	rec := runDrain(t, c, operation("drain", "gpu-0", nil))

	if got := rec.last().Result["accelerator_pods"]; got != "1" {
		t.Fatalf("accelerator_pods = %q, want 1: the node selector is not being applied", got)
	}
	if gone(t, c, "dynamo", "elsewhere") {
		t.Error("draining gpu-0 evicted a pod on gpu-1")
	}
}

// TestDrainReportsProgressPerEviction: a drain takes minutes, so the plane has to see it move.
// One `running` frame per evicted pod, with progress climbing to 1.
func TestDrainReportsProgressPerEviction(t *testing.T) {
	c := drainClient(interceptor.Funcs{},
		node("gpu-0", false),
		acceleratorPod("w0", "gpu-0"),
		acceleratorPod("w1", "gpu-0"),
		acceleratorPod("w2", "gpu-0"),
	)
	rec := runDrain(t, c, operation("drain", "gpu-0", nil))

	rec.mu.Lock()
	defer rec.mu.Unlock()
	var progress []float64
	for _, f := range rec.frames {
		if f.Phase == "running" && strings.Contains(f.Message, "evicted") {
			progress = append(progress, f.Progress)
		}
	}
	if len(progress) != 3 {
		t.Fatalf("saw %d eviction updates, want one per pod: %v", len(progress), progress)
	}
	for i := 1; i < len(progress); i++ {
		if progress[i] <= progress[i-1] {
			t.Errorf("progress did not advance: %v", progress)
			break
		}
	}
	if progress[len(progress)-1] != 1 {
		t.Errorf("final eviction reported progress %v, want 1", progress[len(progress)-1])
	}
}

// TestARefusedEvictionFailsTheDrainRatherThanDeleting is the reason this uses eviction at all: a
// pod at its disruption budget's floor stays put, and the drain says so. Reporting success here
// would be the outage the operation exists to avoid causing.
func TestARefusedEvictionFailsTheDrainRatherThanDeleting(t *testing.T) {
	refuse := interceptor.Funcs{
		SubResourceCreate: func(_ context.Context, _ client.Client, sub string, obj client.Object, _ client.Object, _ ...client.SubResourceCreateOption) error {
			if sub == "eviction" && obj.GetName() == "protected" {
				return apierrors.NewTooManyRequests("disruption budget at its floor", 10)
			}
			return nil
		},
	}
	c := drainClient(refuse,
		node("gpu-0", false),
		acceleratorPod("protected", "gpu-0"),
		acceleratorPod("free", "gpu-0"),
	)
	rec := runDrain(t, c, operation("drain", "gpu-0", nil))

	last := rec.last()
	if last.Phase != "failed" {
		t.Fatalf("phase = %q, want failed: a refused eviction is not a drained node", last.Phase)
	}
	if last.Failure == nil || !strings.Contains(last.Failure.Message, "could not be evicted") {
		t.Errorf("failure does not say what happened: %+v", last.Failure)
	}
	// The partial result still travels, so the plane knows how far it got.
	if last.Result["evicted_replicas"] != "1" || last.Result["remaining_replicas"] != "1" {
		t.Errorf("result = %v, want 1 evicted and 1 remaining", last.Result)
	}
	if gone(t, c, "dynamo", "protected") {
		t.Error("the pod the API server refused to evict was removed anyway")
	}
}

// TestDrainCarriesTheGracePeriodIntoTheEviction: the parameter is the only way a caller can ask
// for a longer shutdown, and a drain that dropped it would kill a model mid-request.
func TestDrainCarriesTheGracePeriodIntoTheEviction(t *testing.T) {
	var seen *int64
	capture := interceptor.Funcs{
		SubResourceCreate: func(ctx context.Context, c client.Client, sub string, obj client.Object, subObj client.Object, opts ...client.SubResourceCreateOption) error {
			if ev, ok := subObj.(*policyv1.Eviction); ok && ev.DeleteOptions != nil {
				seen = ev.DeleteOptions.GracePeriodSeconds
			}
			return c.SubResource(sub).Create(ctx, obj, subObj, opts...)
		},
	}
	c := drainClient(capture, node("gpu-0", false), acceleratorPod("w0", "gpu-0"))
	rec := runDrain(t, c, operation("drain", "gpu-0", map[string]string{"grace_period_seconds": "45"}))

	if rec.last().Phase != "succeeded" {
		t.Fatalf("phase = %q, want succeeded", rec.last().Phase)
	}
	if seen == nil || *seen != 45 {
		t.Fatalf("eviction carried grace period %v, want 45", seen)
	}
	if got := rec.last().Result["grace_period_second"]; got != "45" {
		t.Errorf("result grace period = %q, want 45", got)
	}
}

// TestAnUnparseableGracePeriodFallsBackRatherThanFailing: the parameter arrives as a string off
// the wire, and a drain is too expensive to refuse over a malformed optional field.
func TestAnUnparseableGracePeriodFallsBackRatherThanFailing(t *testing.T) {
	var carried bool
	capture := interceptor.Funcs{
		SubResourceCreate: func(ctx context.Context, c client.Client, sub string, obj client.Object, subObj client.Object, opts ...client.SubResourceCreateOption) error {
			if ev, ok := subObj.(*policyv1.Eviction); ok && ev.DeleteOptions != nil {
				carried = true
			}
			return c.SubResource(sub).Create(ctx, obj, subObj, opts...)
		},
	}
	c := drainClient(capture, node("gpu-0", false), acceleratorPod("w0", "gpu-0"))
	rec := runDrain(t, c, operation("drain", "gpu-0", map[string]string{"grace_period_seconds": "soon"}))

	if rec.last().Phase != "succeeded" {
		t.Fatalf("phase = %q, want succeeded", rec.last().Phase)
	}
	if carried {
		t.Error("a malformed grace period reached the eviction instead of falling back to the default")
	}
}

// TestDrainWithNoAcceleratorPodsSucceedsEmpty: a node with nothing serving on it is already
// drained, and must not be reported as a failure.
func TestDrainWithNoAcceleratorPodsSucceedsEmpty(t *testing.T) {
	c := drainClient(interceptor.Funcs{}, node("gpu-0", false), plainPod("csi-driver", "gpu-0"))
	rec := runDrain(t, c, operation("drain", "gpu-0", nil))

	last := rec.last()
	if last.Phase != "succeeded" {
		t.Fatalf("phase = %q, want succeeded", last.Phase)
	}
	if last.Result["accelerator_pods"] != "0" || last.Result["evicted_replicas"] != "0" {
		t.Errorf("result = %v, want an empty drain", last.Result)
	}
}

// TestDrainFailsWhenPodsCannotBeListed: an unreachable API server must surface as a failure, not
// read as a node that turned out to be empty.
func TestDrainFailsWhenPodsCannotBeListed(t *testing.T) {
	boom := interceptor.Funcs{
		List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			if _, ok := list.(*corev1.PodList); ok {
				return errors.New("apiserver unreachable")
			}
			return c.List(ctx, list, opts...)
		},
	}
	c := drainClient(boom, node("gpu-0", false), acceleratorPod("w0", "gpu-0"))
	rec := runDrain(t, c, operation("drain", "gpu-0", nil))

	last := rec.last()
	if last.Phase != "failed" {
		t.Fatalf("phase = %q, want failed", last.Phase)
	}
	if last.Failure == nil || !strings.Contains(last.Failure.Message, "list pods") {
		t.Errorf("failure does not name what went wrong: %+v", last.Failure)
	}
	if gone(t, c, "dynamo", "w0") {
		t.Error("the pod was evicted despite the list failing")
	}
}

// A disruption budget that refuses for a while and then relents is waited out, not reported as a
// failed drain.
func TestARefusedEvictionIsRetriedUntilTheBudgetAllowsIt(t *testing.T) {
	refusals := 0
	relenting := interceptor.Funcs{
		SubResourceCreate: func(ctx context.Context, c client.Client, sub string, obj client.Object, subObj client.Object, opts ...client.SubResourceCreateOption) error {
			if sub == "eviction" && refusals < 2 {
				refusals++
				return apierrors.NewTooManyRequests("disruption budget at its floor", 1)
			}
			return c.SubResource(sub).Create(ctx, obj, subObj, opts...)
		},
	}
	c := drainClient(relenting, node("gpu-0", false), acceleratorPod("w0", "gpu-0"))
	rec := runDrain(t, c, operation("drain", "gpu-0", nil))

	if last := rec.last(); last.Phase != "succeeded" {
		t.Fatalf("phase = %q (%+v), want succeeded after the budget relented", last.Phase, last.Failure)
	}
	if refusals != 2 {
		t.Errorf("saw %d refusals, want the two the budget gave", refusals)
	}
	if !gone(t, c, "dynamo", "w0") {
		t.Error("the pod survived the drain")
	}
}

// An accepted eviction is not an empty node: the drain waits for the pod to exit.
func TestADrainWaitsForEvictedPodsToExit(t *testing.T) {
	lingering := interceptor.Funcs{
		SubResourceCreate: func(context.Context, client.Client, string, client.Object, client.Object, ...client.SubResourceCreateOption) error {
			return nil // accepted, but the pod never terminates
		},
	}
	c := drainClient(lingering, node("gpu-0", false), acceleratorPod("w0", "gpu-0"))
	rec := runDrain(t, c, operation("drain", "gpu-0", nil))

	last := rec.last()
	if last.Phase != "failed" || last.Failure == nil || !strings.Contains(last.Failure.Message, "had not exited") {
		t.Fatalf("frame = %+v, want a failure saying the pod had not exited", last)
	}
	if last.Result["exiting_replicas"] != "1" {
		t.Errorf("result = %v, want one pod still exiting", last.Result)
	}
}

// Accelerator pods this operator did not place are left alone unless the drain is forced.
func TestADrainLeavesUnmanagedAcceleratorPodsUnlessForced(t *testing.T) {
	c := drainClient(interceptor.Funcs{},
		node("gpu-0", false),
		acceleratorPod("ours", "gpu-0"),
		unmanagedAcceleratorPod("theirs", "gpu-0"),
	)
	rec := runDrain(t, c, operation("drain", "gpu-0", nil))
	if last := rec.last(); last.Phase != "succeeded" || last.Result["unmanaged_accelerator_pods"] != "1" {
		t.Fatalf("frame = %+v, want success reporting one unmanaged pod left", last)
	}
	if gone(t, c, "dynamo", "theirs") {
		t.Error("the drain evicted a pod this operator did not place")
	}

	forced := operation("drain", "gpu-0", map[string]string{"force": "true"})
	forced.OperationID = "opr_01j9x2forcedforcedforcedff"
	runDrain(t, c, forced)
	if !gone(t, c, "dynamo", "theirs") {
		t.Error("a forced drain left an accelerator pod behind")
	}
}
