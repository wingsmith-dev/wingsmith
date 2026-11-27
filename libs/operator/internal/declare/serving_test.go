package declare

import (
	"context"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

func planeStatus(ready string, drift bool) map[string]any {
	conds := []any{
		map[string]any{"type": "Ready", "status": ready, "reason": "ReplicasNotReady", "message": "1 of 3 ready"},
		map[string]any{"type": "Unschedulable", "status": "False", "reason": "Placed"},
	}
	if ready == "True" {
		conds[0] = map[string]any{"type": "Ready", "status": "True", "reason": "ReplicasReady", "message": "3 of 3 ready"}
	}
	if drift {
		conds = append(conds, map[string]any{"type": "PlacementDrift", "status": "True", "reason": "OutsideAllowedClusters", "message": "replicas run on beta"})
	}
	return map[string]any{
		"scale": map[string]any{"placed": float64(3), "ready": float64(1)},
		"deployments": []any{
			map[string]any{"cluster": "alpha", "desiredReplicas": float64(2), "readyReplicas": float64(1)},
			map[string]any{"cluster": "beta", "desiredReplicas": float64(1), "readyReplicas": float64(0)},
			map[string]any{"cluster": "alpha", "desiredReplicas": float64(0), "readyReplicas": float64(0)},
		},
		"conditions": conds,
	}
}

func TestServingStatusFromThePlane(t *testing.T) {
	r, c, plane := setup(t, namespace("team-a", "search"), workload("team-a", "chat", spec()))
	plane.status = planeStatus("False", true)
	reconcileOnce(t, r, "team-a", "chat")

	u := get(t, c, "team-a", "chat")
	if s := condition(t, u, ConditionReady); s == nil || s.Status != metav1.ConditionFalse || s.Reason != "ReplicasNotReady" {
		t.Fatalf("Ready = %+v", s)
	}
	if s := condition(t, u, ConditionReconciling); s == nil || s.Status != metav1.ConditionTrue {
		t.Fatalf("Reconciling = %+v", s)
	}
	if s := condition(t, u, ConditionPlacementDrift); s == nil || s.Status != metav1.ConditionTrue {
		t.Fatalf("PlacementDrift = %+v", s)
	}
	if s := condition(t, u, ConditionStalled); s != nil {
		t.Fatalf("Stalled = %+v", s)
	}
	placed, _, _ := unstructured.NestedSlice(u.Object, "status", "placement", "clusters")
	if len(placed) != 2 {
		t.Fatalf("clusters = %v", placed)
	}
	alpha := placed[0].(map[string]any)
	if alpha["cluster"] != "alpha" || alpha["desiredReplicas"] != int64(2) || alpha["readyReplicas"] != int64(1) {
		t.Fatalf("alpha = %v", alpha)
	}
	if ready, _, _ := unstructured.NestedFieldNoCopy(u.Object, "status", "replicas", "ready"); ready == nil {
		t.Fatalf("replicas = %v", u.Object["status"])
	}

	// Serving catches up and the drift is gone: Ready, nothing in progress.
	plane.status = planeStatus("True", false)
	reconcileOnce(t, r, "team-a", "chat")
	u = get(t, c, "team-a", "chat")
	if s := condition(t, u, ConditionReady); s == nil || s.Status != metav1.ConditionTrue {
		t.Fatalf("Ready = %+v", s)
	}
	if s := condition(t, u, ConditionReconciling); s != nil {
		t.Fatalf("Reconciling = %+v", s)
	}
	if s := condition(t, u, ConditionPlacementDrift); s != nil {
		t.Fatalf("PlacementDrift = %+v", s)
	}
}

func endpoint(ns, name string) *unstructured.Unstructured {
	u := newObject(EndpointGVK)
	u.SetNamespace(ns)
	u.SetName(name)
	u.SetGeneration(1)
	u.SetUID(types.UID(ns + "-" + name))
	u.Object["spec"] = map[string]any{
		"slug":           "chat",
		"deletionPolicy": "Delete",
		"targets":        []any{map[string]any{"workloadRef": map[string]any{"name": "chat"}}},
	}
	return u
}

func TestEndpointIsAppliedWithoutPlacementAndReportsRoutable(t *testing.T) {
	r, c, plane := setup(t, namespace("team-a", "search"), endpoint("team-a", "chat"))
	r.GVK = EndpointGVK
	plane.status = map[string]any{
		"targets":    []any{map[string]any{"deployment": "chat-alpha", "deploymentId": "dep_1", "workload": "chat"}},
		"conditions": []any{map[string]any{"type": "Ready", "status": "True", "reason": "Routable", "message": "1 of 1 targets serving"}},
	}
	reconcileOnce(t, r, "team-a", "chat")

	sent := plane.applies[0]
	if sent.Kind != "Endpoint" || sent.Body["kind"] != "Endpoint" {
		t.Fatalf("sent %+v", sent)
	}
	if _, found, _ := unstructured.NestedFieldNoCopy(sent.Body, "spec", "placement"); found {
		t.Fatal("an endpoint got a placement default")
	}
	if _, found, _ := unstructured.NestedFieldNoCopy(sent.Body, "spec", "deletionPolicy"); found {
		t.Fatal("deletionPolicy was sent to the plane")
	}
	u := newObject(EndpointGVK)
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "team-a", Name: "chat"}, u); err != nil {
		t.Fatal(err)
	}
	if s := condition(t, u, ConditionReady); s == nil || s.Status != metav1.ConditionTrue || s.Reason != "Routable" {
		t.Fatalf("Ready = %+v", s)
	}
	if _, found, _ := unstructured.NestedFieldNoCopy(u.Object, "status", "placement"); found {
		t.Fatal("an endpoint reported placement")
	}
	targets, _, _ := unstructured.NestedSlice(u.Object, "status", "targets")
	if len(targets) != 1 || targets[0].(map[string]any)["workload"] != "chat" {
		t.Fatalf("targets = %v", targets)
	}

	// Deleting it deletes the plane endpoint first.
	if err := c.Delete(context.Background(), u); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, "team-a", "chat")
	if len(plane.deletes) != 1 || plane.deletes[0].Kind != "Endpoint" {
		t.Fatalf("deletes = %+v", plane.deletes)
	}
}

func TestChangedFramesReachTheirKind(t *testing.T) {
	ch := NewChannel(time.Second)
	payload := []byte(`{"kind":"Endpoint","project":"search","name":"chat","namespace":"team-a"}`)
	if err := WithChannel(nil, ch).Handle(context.Background(), &syncpkg.Envelope{Type: TypeChanged, Payload: payload}); err != nil {
		t.Fatal(err)
	}
	select {
	case m := <-ch.Changes("Endpoint"):
		if m.Namespace != "team-a" || m.Name != "chat" {
			t.Fatalf("changed = %+v", m)
		}
	default:
		t.Fatal("nothing delivered to the Endpoint reconciler")
	}
	select {
	case m := <-ch.Changes("Workload"):
		t.Fatalf("delivered to Workload: %+v", m)
	default:
	}

	r := &Reconciler{GVK: EndpointGVK}
	in := make(chan *wire.ManifestChanged, 1)
	r.Changes = in
	out := make(chan eventOf, 1)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = r.watchChanges(ctx, out) }()
	in <- &wire.ManifestChanged{Kind: "Endpoint", Name: "chat", Namespace: "team-a"}
	select {
	case e := <-out:
		if e.Object.GetNamespace() != "team-a" || e.Object.GetName() != "chat" || e.Object.GetKind() != "Endpoint" {
			t.Fatalf("event = %v", e.Object)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no reconcile queued")
	}
}
