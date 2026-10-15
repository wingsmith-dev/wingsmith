package reconcile

import (
	"context"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
)

// TestReadOnlyReleasesADeclarationDeletedWithKubectl: a declaration deleted outside the hub, as
// the first step of an uninstall is, carries the finalizer from an earlier read-write run and has
// no reconciler to remove it. The releaser does.
func TestReadOnlyReleasesADeclarationDeletedWithKubectl(t *testing.T) {
	s, c := newStore(t, []client.Object{terminating("dep-a", 1)}, nil)
	r := &FinalizerReleaser{Client: c, Log: s.Log}

	key := client.ObjectKey{Namespace: testNS, Name: "dep-a"}
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: key}); err != nil {
		t.Fatal(err)
	}
	if names := localNames(t, c); len(names) != 0 {
		t.Fatalf("the deleted declaration is still terminating: %v", names)
	}
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: key}); err != nil {
		t.Fatalf("a declaration already gone must be a no-op, got %v", err)
	}
}

// TestTheReleaserLeavesALiveDeclarationAlone: only a declaration being deleted is released.
func TestTheReleaserLeavesALiveDeclarationAlone(t *testing.T) {
	live := &v1alpha1.SyncedResource{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNS, Name: "dep-a", Finalizers: []string{renderedFinalizer}},
		Spec:       v1alpha1.SyncedResourceSpec{Kind: "deployment", Revision: 1},
	}
	s, c := newStore(t, []client.Object{live}, nil)
	r := &FinalizerReleaser{Client: c, Log: s.Log}

	key := client.ObjectKey{Namespace: testNS, Name: "dep-a"}
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: key}); err != nil {
		t.Fatal(err)
	}
	var got v1alpha1.SyncedResource
	if err := c.Get(context.Background(), key, &got); err != nil {
		t.Fatal(err)
	}
	if len(got.Finalizers) != 1 {
		t.Fatalf("the finalizer was removed from a live declaration: %v", got.Finalizers)
	}

	p := heldInDeletion()
	if p.Create(event.CreateEvent{Object: live}) {
		t.Error("a live declaration passed the filter")
	}
	if !p.Update(event.UpdateEvent{ObjectOld: live, ObjectNew: terminating("dep-a", 1)}) {
		t.Error("a declaration being deleted with the finalizer was filtered out")
	}
}
