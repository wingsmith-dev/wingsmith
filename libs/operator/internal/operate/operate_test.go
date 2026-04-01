package operate

import (
	"context"
	"encoding/json"
	"log/slog"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/hangar-dev/hangar/libs/operator/internal/observe"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

const testCluster = "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"

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
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "dynamo"},
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
