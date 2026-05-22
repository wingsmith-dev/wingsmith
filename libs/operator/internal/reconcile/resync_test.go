package reconcile

import (
	"context"
	"log/slog"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// streamingReporter records a correlated snapshot the way the hub would receive it.
type streamingReporter struct {
	recordingReporter
	mu     sync.Mutex
	begun  []wire.ResyncBegin
	ended  []wire.StreamEnd
	stream []syncpkg.MessageType
	err    error
}

func (s *streamingReporter) SendCorrelated(
	t syncpkg.MessageType, subject string, payload any, replyTo string,
) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return "", s.err
	}
	switch p := payload.(type) {
	case wire.ResyncBegin:
		s.begun = append(s.begun, p)
		return "req-1", nil
	case wire.StreamEnd:
		s.ended = append(s.ended, p)
		return "", nil
	}
	s.stream = append(s.stream, t)
	return "", s.recordingReporter.Send(t, subject, payload)
}

func demandReconciler(t *testing.T, rep Reporter, objects ...client.Object) *SyncedResourceReconciler {
	t.Helper()
	scheme := clientgoscheme.Scheme
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()
	r := &SyncedResourceReconciler{
		Client: c, Scheme: scheme, Log: slog.Default(),
		Namespace: "hangar-system", Render: testRender,
		ClusterID: "ap-cluster-1",
	}
	r.Bind(rep)
	return r
}

// The gap this closes: nothing ever stated the whole of what a cluster holds, so the plane could
// correct a replica but never forget one. A deployment torn down took its last frame's word for
// ever, and the accelerators under it stayed committed to a pod that no longer existed.
func TestADemandResyncStatesEveryReplicaAndAllocation(t *testing.T) {
	rep := &streamingReporter{}
	objects := []client.Object{
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
		declared(t, "recipe", testRecipeID, testRecipeDesired()),
	}
	for _, pod := range readyPods(2) {
		objects = append(objects, pod)
	}
	r := demandReconciler(t, rep, objects...)

	if err := r.ResyncDemand(context.Background()); err != nil {
		t.Fatalf("resync: %v", err)
	}

	if len(rep.begun) != 1 || rep.begun[0].Scope != demandScope {
		t.Fatalf("opened %+v, want one %q stream", rep.begun, demandScope)
	}
	if len(rep.replicaFrames) != 2 {
		t.Errorf("streamed %d replicas, want 2", len(rep.replicaFrames))
	}
	if len(rep.ended) != 1 {
		t.Fatalf("closed %d streams, want 1", len(rep.ended))
	}
	if !rep.ended[0].Complete {
		t.Error("snapshot is not complete, so the plane may not prune against it")
	}
	if rep.ended[0].Count != len(rep.stream) {
		t.Errorf("declared %d frames, sent %d", rep.ended[0].Count, len(rep.stream))
	}
}

// A deployment with nothing running contributes nothing, and that is the whole point: the plane
// prunes exactly the replicas this snapshot did not name.
func TestADemandResyncOfATornDownDeploymentIsEmptyAndComplete(t *testing.T) {
	rep := &streamingReporter{}
	r := demandReconciler(t, rep,
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
		declared(t, "recipe", testRecipeID, testRecipeDesired()),
	)

	if err := r.ResyncDemand(context.Background()); err != nil {
		t.Fatalf("resync: %v", err)
	}

	if len(rep.replicaFrames) != 0 || len(rep.allocationFrames) != 0 {
		t.Errorf("streamed %d replicas and %d allocations, want none",
			len(rep.replicaFrames), len(rep.allocationFrames))
	}
	if len(rep.ended) != 1 || !rep.ended[0].Complete || rep.ended[0].Count != 0 {
		t.Errorf("closed with %+v, want an empty complete snapshot", rep.ended)
	}
}

// Completeness is the permission to delete, so anything unreadable has to withdraw it. A snapshot
// that claimed to be whole while missing a deployment would delete a running replica's allocation
// and offer its accelerators to somebody else.
func TestADeploymentThatCannotBeReadMakesTheSnapshotIncomplete(t *testing.T) {
	rep := &streamingReporter{}
	// Valid JSON, so the object lists cleanly and the failure is where it belongs: decoding
	// this one deployment. A payload that broke the list would prove something else.
	broken := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	broken.Spec.Payload.Raw = []byte(`{"deployment_id": 12}`)
	r := demandReconciler(t, rep, broken)

	if err := r.ResyncDemand(context.Background()); err != nil {
		t.Fatalf("resync: %v", err)
	}

	if len(rep.ended) != 1 {
		t.Fatalf("closed %d streams, want 1", len(rep.ended))
	}
	if rep.ended[0].Complete {
		t.Error("snapshot claims to be complete although a deployment could not be read")
	}
}

// Nothing about a resync may change what the reconciler decides, and a reporter that cannot open a
// stream is the read-only deployment.
func TestAReporterThatCannotStreamSkipsTheDemandResync(t *testing.T) {
	rep := &recordingReporter{}
	r := demandReconciler(t, rep, declared(t, "deployment", testDeploymentID, testDeploymentDesired()))

	if err := r.ResyncDemand(context.Background()); err != nil {
		t.Fatalf("resync: %v", err)
	}
	if len(rep.sent)+len(rep.replicaFrames)+len(rep.allocationFrames) != 0 {
		t.Error("an unstreamable reporter was sent frames anyway")
	}
}

// An adopted deployment is reported by a different route, and it has to be in the snapshot too —
// otherwise every resync would delete the replicas of everything this operator did not render.
func TestADemandResyncIncludesAdoptedDeployments(t *testing.T) {
	rep := &streamingReporter{}
	res := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	res.Spec.Adopt = &v1alpha1.AdoptRef{Kind: "pod", Namespace: "dynamo", Name: "adopted-0"}
	pod := &corev1.Pod{}
	pod.Name, pod.Namespace = "adopted-0", "dynamo"
	pod.Spec.NodeName = "gpu-0"
	pod.Status.Phase = corev1.PodRunning
	pod.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}
	r := demandReconciler(t, rep, res, pod)

	if err := r.ResyncDemand(context.Background()); err != nil {
		t.Fatalf("resync: %v", err)
	}

	if len(rep.replicaFrames) != 1 {
		t.Fatalf("streamed %d replicas for an adopted deployment, want 1", len(rep.replicaFrames))
	}
	if !rep.ended[0].Complete {
		t.Error("an adopted deployment that read cleanly should not spoil completeness")
	}
}

// The trigger matters as much as the snapshot. Hanging the repair off a fresh session means it
// runs almost never — the hub resumes a stream whenever it can — so the ticker is what makes
// convergence a property of time rather than of a reconnect that may not come.
func TestTheDemandResyncRunsOnItsInterval(t *testing.T) {
	rep := &streamingReporter{}
	r := demandReconciler(t, rep, declared(t, "deployment", testDeploymentID, testDeploymentDesired()))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- r.RunDemandResync(ctx, time.Millisecond) }()

	deadline := time.After(2 * time.Second)
	for {
		rep.mu.Lock()
		sent := len(rep.ended)
		rep.mu.Unlock()
		if sent >= 2 {
			break
		}
		select {
		case <-deadline:
			t.Fatalf("streamed %d snapshots in two seconds, want the ticker to keep firing", sent)
		default:
		}
		time.Sleep(time.Millisecond)
	}

	cancel()
	if err := <-done; err != nil {
		t.Errorf("run returned %v, want a clean stop on cancel", err)
	}
}

// An interval of zero is not "every instant"; it is a caller that does not want the loop.
func TestADemandResyncIntervalOfZeroDoesNotRun(t *testing.T) {
	rep := &streamingReporter{}
	r := demandReconciler(t, rep, declared(t, "deployment", testDeploymentID, testDeploymentDesired()))

	if err := r.RunDemandResync(context.Background(), 0); err != nil {
		t.Fatalf("run: %v", err)
	}
	if len(rep.begun) != 0 {
		t.Errorf("opened %d streams, want none", len(rep.begun))
	}
}
