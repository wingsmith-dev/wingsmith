package operate

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// runOn hands an operation to a fresh executor over an existing client, which is what an
// operator that restarted looks like to the cluster.
func runOn(t *testing.T, c client.Client, opts render.Options, op wire.OperationDesired) wire.OperationObserved {
	t.Helper()
	rec := &recorder{}
	e := New(&Executor{Client: c, ClusterID: testCluster, Log: slog.Default(), Render: opts})
	t.Cleanup(e.Close)
	e.Bind(rec)
	payload, err := json.Marshal(op)
	if err != nil {
		t.Fatal(err)
	}
	if err := e.Handle(context.Background(), &syncpkg.Envelope{Type: TypeOperationDesired, Payload: payload}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if p := rec.phases(); len(p) > 0 && (p[len(p)-1] == "succeeded" || p[len(p)-1] == "failed") {
			return rec.last()
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("operation did not conclude; saw %v", rec.phases())
	return wire.OperationObserved{}
}

func getNode(t *testing.T, c client.Client, name string) *corev1.Node {
	t.Helper()
	var n corev1.Node
	if err := c.Get(context.Background(), types.NamespacedName{Name: name}, &n); err != nil {
		t.Fatal(err)
	}
	return &n
}

// A replay that reaches an operator with no memory of the operation, after a restart, is answered
// from the node's record instead of being performed a second time.
func TestAReplayAfterARestartIsAnsweredFromTheNode(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithObjects(node("gpu-0", false)).Build()
	op := operation("cordon", "gpu-0", nil)
	if got := runOn(t, c, render.Options{}, op); got.Phase != "succeeded" {
		t.Fatalf("phase = %q, want succeeded", got.Phase)
	}

	n := getNode(t, c, "gpu-0")
	n.Spec.Unschedulable = false
	if err := c.Update(context.Background(), n); err != nil {
		t.Fatal(err)
	}

	got := runOn(t, c, render.Options{}, op)
	if got.Phase != "succeeded" || got.Result["replayed"] != "true" {
		t.Fatalf("replay = %+v, want the recorded success", got)
	}
	if getNode(t, c, "gpu-0").Spec.Unschedulable {
		t.Error("the replay cordoned the node again")
	}
}

func TestTheNodeRecordIsBounded(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithObjects(node("gpu-0", false)).Build()
	e := New(&Executor{Client: c, Log: discardLogger()})
	t.Cleanup(e.Close)
	for i := 0; i < maxNodeOperations+3; i++ {
		e.recordOperation("gpu-0", fmt.Sprintf("opr_%d", i), "succeeded")
	}
	n := getNode(t, c, "gpu-0")
	if entries := strings.Fields(n.Annotations[nodeOperationsAnnotation]); len(entries) != maxNodeOperations {
		t.Fatalf("record holds %d entries, want %d", len(entries), maxNodeOperations)
	}
	if _, ok := recordedOperation(n, "opr_0"); ok {
		t.Error("the oldest entry was kept past the bound")
	}
	if phase, ok := recordedOperation(n, fmt.Sprintf("opr_%d", maxNodeOperations+2)); !ok || phase != "succeeded" {
		t.Error("the newest entry was not kept")
	}
}

// Cordon and drain reach only the GPU pool unless forced: a wrong node id must not be enough to
// take a system node out of service.
func TestNodeOperationsStayInTheGPUPoolUnlessForced(t *testing.T) {
	pool := render.Options{PoolLabelKey: "hangar.dev/pool", GPUPoolValue: "gpu"}
	gpu := node("gpu-0", false)
	gpu.Labels = map[string]string{"hangar.dev/pool": "gpu"}
	system := node("sys-0", false)
	system.Labels = map[string]string{"hangar.dev/pool": "system"}
	c := fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithObjects(gpu, system).Build()

	if got := runOn(t, c, pool, operation("cordon", "gpu-0", nil)); got.Phase != "succeeded" {
		t.Errorf("cordoning a GPU node: %+v", got)
	}

	got := runOn(t, c, pool, operation("cordon", "sys-0", nil))
	if got.Phase != "failed" || got.Failure == nil || !strings.Contains(got.Failure.Message, "force=true") {
		t.Errorf("cordoning a system node = %+v, want a refusal naming force", got)
	}
	if getNode(t, c, "sys-0").Spec.Unschedulable {
		t.Error("the system node was cordoned")
	}

	forced := operation("cordon", "sys-0", map[string]string{"force": "true"})
	forced.OperationID = "opr_01j9x2ffffffffffffffffffff"
	if got := runOn(t, c, pool, forced); got.Phase != "succeeded" {
		t.Errorf("a forced cordon of a system node: %+v", got)
	}
}
