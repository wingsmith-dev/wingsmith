package reconcile

import (
	"context"
	"errors"
	"log/slog"
	"reflect"
	"slices"
	"testing"

	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// recordingReporter keeps what the reconciler tried to tell the hub.
type recordingReporter struct {
	sent             []wire.DeploymentObserved
	replicaFrames    []wire.ReplicaObserved
	allocationFrames []wire.AllocationObserved
	cacheFrames      []wire.CacheObserved
}

func (r *recordingReporter) Send(_ syncpkg.MessageType, _ string, payload any) error {
	switch p := payload.(type) {
	case wire.DeploymentObserved:
		r.sent = append(r.sent, p)
	case wire.ReplicaObserved:
		r.replicaFrames = append(r.replicaFrames, p)
	case wire.AllocationObserved:
		r.allocationFrames = append(r.allocationFrames, p)
	case wire.CacheObserved:
		r.cacheFrames = append(r.cacheFrames, p)
	}
	return nil
}

// lastSent is the most recent thing said about the deployment, which is what a reader of the
// console would be looking at.
func lastSent(t *testing.T, r *recordingReporter) wire.DeploymentObserved {
	t.Helper()
	if len(r.sent) == 0 {
		t.Fatal("nothing was reported about the deployment")
	}
	return r.sent[len(r.sent)-1]
}

func runReconcileReporting(t *testing.T, rep Reporter, objects ...client.Object) (*SyncedResourceReconciler, ctrl.Result) {
	t.Helper()
	scheme := clientgoscheme.Scheme
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	subject := objects[0]
	c := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(objects...).WithStatusSubresource(subject).Build()
	r := &SyncedResourceReconciler{
		Client: c, Scheme: scheme, Log: slog.Default(),
		Namespace: "hangar-system", Render: testRender,
		ClusterID: "ap-cluster-1",
	}
	r.Bind(rep)

	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: subject.GetNamespace(), Name: subject.GetName()}}
	result, err := r.Reconcile(context.Background(), req)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	return r, result
}

// The gap this closes: the operator knew exactly why a deployment was not serving and said so in a
// log line in a cluster nobody was reading, while the plane showed "placed" and nothing else.
func TestWaitingIsReportedToTheHub(t *testing.T) {
	rep := &recordingReporter{}
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	runReconcileReporting(t, rep, dep)

	// Two: the instruction arriving, then what the reconcile concluded about it.
	if len(rep.sent) != 2 {
		t.Fatalf("sent %d frames, want 2", len(rep.sent))
	}
	if got := rep.sent[0].Reason; got != "Accepted" {
		t.Errorf("first reason = %q, want Accepted", got)
	}
	got := rep.sent[1]
	if got.Reason != "ArtifactNotDeclared" {
		t.Errorf("reason = %q, want ArtifactNotDeclared", got.Reason)
	}
	if got.Phase != phasePending {
		t.Errorf("phase = %q, want %q", got.Phase, phasePending)
	}
	if got.Message == "" {
		t.Error("a reason with no message tells a person nothing they could act on")
	}
	if got.DeploymentID != testDeploymentID {
		t.Errorf("deployment = %q, want %q", got.DeploymentID, testDeploymentID)
	}
	if got.ClusterID != "ap-cluster-1" {
		t.Errorf("cluster = %q, want the operator's own", got.ClusterID)
	}
}

// A waiting deployment is re-examined every fifteen seconds and concludes the same thing almost
// every time. Sending each pass would spend the stream on a fact that has not moved.
func TestAnUnchangedConclusionIsNotResent(t *testing.T) {
	rep := &recordingReporter{}
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	r, _ := runReconcileReporting(t, rep, dep)

	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: dep.GetNamespace(), Name: dep.GetName()}}
	for i := 0; i < 3; i++ {
		if _, err := r.Reconcile(context.Background(), req); err != nil {
			t.Fatalf("reconcile: %v", err)
		}
	}

	// `Accepted` once and the conclusion once, over four passes. `Accepted` is true on every one
	// of them, and reporting it each time would alternate with the wait reason forever.
	if len(rep.sent) != 2 {
		t.Fatalf("sent %d frames over four passes, want 2", len(rep.sent))
	}
	if rep.sent[0].Reason != "Accepted" || rep.sent[1].Reason != "ArtifactNotDeclared" {
		t.Fatalf("frames = %q, %q", rep.sent[0].Reason, rep.sent[1].Reason)
	}
}

// A bumped revision is a new instruction, and the cluster receiving it is the fact the strip's
// first step shows. Remembering `Accepted` across revisions would report it only for the first
// version of a deployment a process ever saw.
func TestANewRevisionReportsItsArrival(t *testing.T) {
	rep := &recordingReporter{}
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	r, _ := runReconcileReporting(t, rep, dep)

	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: dep.GetNamespace(), Name: dep.GetName()}}
	if err := r.Client.Get(context.Background(), req.NamespacedName, dep); err != nil {
		t.Fatalf("get: %v", err)
	}
	dep.Spec.Revision++
	if err := r.Client.Update(context.Background(), dep); err != nil {
		t.Fatalf("update: %v", err)
	}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	var accepted int
	for _, frame := range rep.sent {
		if frame.Reason == "Accepted" {
			accepted++
		}
	}
	if accepted != 2 {
		t.Fatalf("reported Accepted %d times over two revisions, want 2", accepted)
	}
}

// A deployment recreated at the same id must not be judged a repeat of what it was before it was
// deleted, or its first report is silently dropped.
func TestADeletedDeploymentForgetsWhatItLastReported(t *testing.T) {
	rep := &recordingReporter{}
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	r, _ := runReconcileReporting(t, rep, dep)

	if err := r.Client.Delete(context.Background(), dep); err != nil {
		t.Fatalf("delete: %v", err)
	}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: dep.GetNamespace(), Name: dep.GetName()}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("reconcile after delete: %v", err)
	}

	if got := r.lastReported(testDeploymentID); got != (reportKey{}) {
		t.Errorf("last reported = %+v, want it forgotten", got)
	}
}

// An observed deployment is held, not applied, and that too is a state worth reporting: the
// plane otherwise cannot tell "held because authority says so" from "stuck".
func TestHeldAsObservedIsReported(t *testing.T) {
	rep := &recordingReporter{}
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	dep.Spec.Authority = v1alpha1.AuthorityObserved
	runReconcileReporting(t, rep, dep)

	if len(rep.sent) != 1 {
		t.Fatalf("sent %d frames, want 1", len(rep.sent))
	}
	if got := rep.sent[0].Reason; got != "NoClusterWork" {
		t.Errorf("reason = %q, want NoClusterWork", got)
	}
}

// Nothing about reporting may change what the reconciler decides. An unbound reporter is the
// read-only deployment and every existing test.
func TestAnUnboundReporterReconcilesNormally(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	_, out, result := runReconcile(t, dep)

	if got := readyCondition(t, out).Reason; got != "ArtifactNotDeclared" {
		t.Errorf("reason = %q, want ArtifactNotDeclared", got)
	}
	if result.RequeueAfter == 0 {
		t.Error("a deployment waiting on its artifact must still ask to be called again")
	}
}

func TestNameToIDInvertsResourceName(t *testing.T) {
	if got := nameToID(resourceName(testDeploymentID)); got != testDeploymentID {
		t.Errorf("nameToID(resourceName(%q)) = %q", testDeploymentID, got)
	}
}

// A session the hub could not resume is one where the dedupe's assumption (that a skipped frame
// was delivered) does not hold: the hub prunes what it had and expects it restated.
func TestAFreshSessionForgetsWhatWasReported(t *testing.T) {
	rep := &recordingReporter{}
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	r, _ := runReconcileReporting(t, rep, dep)

	if err := WithReporter(noopHandler{}, r).Resync(context.Background()); err != nil {
		t.Fatalf("resync: %v", err)
	}
	if got := r.lastReported(testDeploymentID); got != (reportKey{}) {
		t.Errorf("last reported = %+v, want it forgotten", got)
	}

	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: dep.GetNamespace(), Name: dep.GetName()}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	// The conclusion again, and only it: `Accepted` is a transition this revision already
	// passed, and replaying it would file a step backwards for a deployment that had moved on.
	if len(rep.sent) != 3 {
		t.Fatalf("sent %d frames, want the conclusion restated after the resync", len(rep.sent))
	}
	if got := rep.sent[2].Reason; got != "ArtifactNotDeclared" {
		t.Errorf("restated reason = %q, want ArtifactNotDeclared", got)
	}
}

// A read-only operator has no reconciler, and the chain must still be usable.
func TestWithReporterIsANoOpWithoutAReconciler(t *testing.T) {
	inner := noopHandler{}
	if got := WithReporter(inner, nil); got != syncpkg.Handler(inner) {
		t.Error("a nil reconciler must leave the chain untouched")
	}
}

type noopHandler struct{}

func (noopHandler) Handle(context.Context, *syncpkg.Envelope) error { return nil }
func (noopHandler) Resync(context.Context) error                    { return nil }

// chainSpy stands in for the store below the reporting handler.
type chainSpy struct {
	handled  int
	resyncs  int
	handleEr error
	resyncEr error
}

func (s *chainSpy) Handle(_ context.Context, _ *syncpkg.Envelope) error {
	s.handled++
	return s.handleEr
}

func (s *chainSpy) Resync(_ context.Context) error {
	s.resyncs++
	return s.resyncEr
}

// TestWithReporterIsAPassThroughForOrdinaryFrames: it exists for the Resync hook and must not
// otherwise change how declared state is handled.
func TestWithReporterIsAPassThroughForOrdinaryFrames(t *testing.T) {
	inner := &chainSpy{}
	r := &SyncedResourceReconciler{reported: map[string]reportKey{}}
	h := WithReporter(inner, r)

	if err := h.Handle(context.Background(), &syncpkg.Envelope{Type: wireDeploymentDesired}); err != nil {
		t.Fatalf("handle: %v", err)
	}
	if inner.handled != 1 {
		t.Errorf("the store saw %d frames, want 1", inner.handled)
	}
}

func TestWithReporterDoesNotSwallowTheStoresError(t *testing.T) {
	inner := &chainSpy{handleEr: errors.New("write failed")}
	h := WithReporter(inner, &SyncedResourceReconciler{reported: map[string]reportKey{}})
	if err := h.Handle(context.Background(), &syncpkg.Envelope{Type: wireDeploymentDesired}); err == nil {
		t.Fatal("the error was swallowed; the hub would not replay the frame")
	}
}

// TestAFreshSessionForgetsWhatWasAlreadySaid is the bug this handler was added for: the dedupe in
// report assumes the frame it skipped was delivered, and a session the hub could not resume is one
// where that assumption does not hold.
func TestAFreshSessionForgetsWhatWasAlreadySaid(t *testing.T) {
	inner := &chainSpy{}
	r := &SyncedResourceReconciler{reported: map[string]reportKey{
		"dep-01j9x2gggggggggggggggggggg": {},
	}}
	h := WithReporter(inner, r)

	if err := h.Resync(context.Background()); err != nil {
		t.Fatalf("resync: %v", err)
	}
	if inner.resyncs != 1 {
		t.Errorf("the store saw %d resyncs, want 1", inner.resyncs)
	}
	if len(r.reported) != 0 {
		t.Error("a fresh session kept the conclusions it had already sent; they will never be restated")
	}
}

// TestAFailedResyncKeepsWhatWasSaid: forgetting after a resync the store refused would restate
// everything against a stream that is not ready for it.
func TestAFailedResyncKeepsWhatWasSaid(t *testing.T) {
	inner := &chainSpy{resyncEr: errors.New("prune failed")}
	r := &SyncedResourceReconciler{reported: map[string]reportKey{"dep-x": {}}}
	h := WithReporter(inner, r)

	if err := h.Resync(context.Background()); err == nil {
		t.Fatal("a failing resync was reported as successful")
	}
	if len(r.reported) != 1 {
		t.Error("state was forgotten even though the resync failed")
	}
}

// TestWithReporterOnANilReconcilerIsTheStoreItself: the operator can run without the reconciler,
// and wrapping nil must not introduce a handler that dereferences it.
func TestWithReporterOnANilReconcilerIsTheStoreItself(t *testing.T) {
	inner := &chainSpy{}
	if got := WithReporter(inner, nil); got != syncpkg.Handler(inner) {
		t.Fatal("wrapping a nil reconciler did not return the store unchanged")
	}
}

// TestForgetAllIsSafeToCallTwice: a hub that opens two fresh sessions in a row must not panic.
func TestForgetAllIsSafeToCallTwice(t *testing.T) {
	r := &SyncedResourceReconciler{reported: map[string]reportKey{"dep-x": {}}}
	r.ForgetAll()
	r.ForgetAll()
	if len(r.reported) != 0 {
		t.Error("ForgetAll left state behind")
	}
}

// The prune the plane performs is only as good as the set this frame carries, and there is one
// failure mode that matters: a pod replaced one-for-one moves no count at all. Without the replica
// set in the suppression key that frame is dropped as unchanged, the plane never hears that the
// old replica is gone, and its row keeps counting towards the rollup for ever, which is how a
// deployment of one came to read three ready of one.
func TestAReplacedPodIsReportedEvenThoughNoCountMoved(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	objects := []client.Object{dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true)}
	for _, pod := range readyPods(2) {
		objects = append(objects, pod)
	}

	reporter := &recordingReporter{}
	r, _ := runReconcileReporting(t, reporter, objects...)
	before := lastSent(t, reporter)
	if len(before.ReplicaIDs) != 2 {
		t.Fatalf("reported %d replica ids, want 2", len(before.ReplicaIDs))
	}

	ctx := context.Background()
	gone := readyPods(2)[1]
	if err := r.Client.Delete(ctx, gone); err != nil {
		t.Fatalf("deleting the replaced pod: %v", err)
	}
	replacement := readyPods(2)[1]
	replacement.Name += "-replaced"
	replacement.ResourceVersion = ""
	if err := r.Client.Create(ctx, replacement); err != nil {
		t.Fatalf("creating the replacement pod: %v", err)
	}

	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "hangar-system", Name: dep.Name}}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("second pass: %v", err)
	}

	after := lastSent(t, reporter)
	if after.ReadyReplicas != before.ReadyReplicas {
		t.Fatalf("the counts moved, so this is not the case under test")
	}
	if slices.Equal(after.ReplicaIDs, before.ReplicaIDs) {
		t.Error("the replaced pod was never reported: the plane cannot prune what it is not told about")
	}
}

// A deployment with no pods yet has to be able to say so, because an empty claim and no claim at
// all mean opposite things to the prune on the other end, and the frames that never looked at a
// pod must make no claim, or a deployment waiting on its artifact would have its replicas deleted.
func TestOnlyTheFrameReadBackFromPodsClaimsAReplicaSet(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())

	waiting := &recordingReporter{}
	runReconcileReporting(t, waiting, dep)
	for _, sent := range waiting.sent {
		if sent.ReplicaIDs != nil {
			t.Errorf("a deployment waiting on its artifact claimed %v replicas", sent.ReplicaIDs)
		}
	}

	rolled := &recordingReporter{}
	runReconcileReporting(t, rolled, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))
	var claimed bool
	for _, sent := range rolled.sent {
		claimed = claimed || sent.ReplicaIDs != nil
	}
	if !claimed {
		t.Error("nothing claimed a replica set, so the plane has nothing to prune against")
	}
}

// The gap this closes: demand state is never pruned against a resync, and the per-deployment prune
// rides on a frame the operator stops sending the moment the deployment is torn down. So a paused
// workload's replicas stayed `ready` in the plane for ever, and the cards they were on stayed held.
func TestTeardownClaimsAnEmptyReplicaSet(t *testing.T) {
	rep := &recordingReporter{}
	r, dep := runRenderedReporting(t, rep)
	tearDown(t, r, dep)

	last := lastSent(t, rep)
	if last.Phase != phaseTerminating {
		t.Errorf("phase = %q, want %q", last.Phase, phaseTerminating)
	}
	if last.ReplicaIDs == nil {
		t.Fatal("replica_ids is nil, which is the frame that makes no claim: teardown has to claim none")
	}
	if len(last.ReplicaIDs) != 0 {
		t.Errorf("replica_ids = %v, want empty", last.ReplicaIDs)
	}
	if last.ReadyReplicas != 0 {
		t.Errorf("ready = %d, want 0", last.ReadyReplicas)
	}
}

// The other half of the same gap: an allocation is held for as long as it is reported, and a
// deployment that goes away stops reporting instead of releasing. The devices stayed committed.
func TestTeardownReleasesWhatItWasHolding(t *testing.T) {
	rep := &recordingReporter{}
	r, dep := runRenderedReporting(t, rep)
	r.markHolding(testDeploymentID, map[string]wire.AllocationObserved{
		"alc_1": {
			AllocationID: "alc_1",
			ClusterID:    "ap-cluster-1",
			DeploymentID: testDeploymentID,
			ProjectID:    "prj_1",
			ReplicaID:    "rep_1",
			NodeID:       "nod_1",
			DeviceIDs:    []string{"dev_1"},
		},
	})
	before := len(rep.allocationFrames)
	tearDown(t, r, dep)

	released := rep.allocationFrames[before:]
	if len(released) != 1 {
		t.Fatalf("sent %d allocation frames on teardown, want 1", len(released))
	}
	if released[0].AllocationID != "alc_1" {
		t.Errorf("allocation = %q, want alc_1", released[0].AllocationID)
	}
	if released[0].ProjectID != "prj_1" {
		t.Errorf("project = %q, want prj_1: a release with no project is dropped on arrival", released[0].ProjectID)
	}
	if released[0].DeviceIDs == nil || len(released[0].DeviceIDs) != 0 {
		t.Errorf("device_ids = %v, want empty and not nil", released[0].DeviceIDs)
	}
}

// runRenderedReporting reconciles a deployment far enough that it actually renders, which is what
// puts the teardown finalizer on the declared copy. A deployment still waiting on its artifact never
// rendered and has nothing to tear down.
func runRenderedReporting(t *testing.T, rep Reporter) (*SyncedResourceReconciler, *v1alpha1.SyncedResource) {
	t.Helper()
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, _ := runReconcileReporting(t, rep, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	var out v1alpha1.SyncedResource
	key := types.NamespacedName{Namespace: dep.GetNamespace(), Name: dep.GetName()}
	if err := r.Client.Get(context.Background(), key, &out); err != nil {
		t.Fatalf("read back the declared copy: %v", err)
	}
	if len(out.Finalizers) == 0 {
		t.Fatal("nothing was rendered, so there is no teardown to observe")
	}
	return r, &out
}

func tearDown(t *testing.T, r *SyncedResourceReconciler, dep *v1alpha1.SyncedResource) {
	t.Helper()
	ctx := context.Background()
	if err := r.Client.Delete(ctx, dep); err != nil {
		t.Fatalf("delete: %v", err)
	}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: dep.Namespace, Name: dep.Name}}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("reconcile after delete: %v", err)
	}
}

// The split reaches the frame, and a role moving inside a fixed total is not suppressed.
//
// The suppression key exists so a deployment re-examined every fifteen seconds does not spend the
// stream restating a fact that has not moved. A prefill worker dying as a decode worker comes
// ready moves nothing the key used to hold (same phase, same reason, same `2 of 3`) and takes
// the deployment from degraded-and-still-serving to serving nothing, which is the one transition
// the frame exists to carry.
func TestARoleMovingInsideAFixedTotalIsNotSuppressed(t *testing.T) {
	rep := &recordingReporter{}
	r := &SyncedResourceReconciler{Log: slog.Default(), ClusterID: "ap-cluster-1"}
	r.Bind(rep)
	dep := disaggregatedDesired()

	first := rollout{
		desired: 3, ready: 2, roleShort: true,
		roles: []wire.RoleObserved{
			{Role: "prefill", Desired: 1, Ready: 1},
			{Role: "decode", Desired: 2, Ready: 1},
		},
	}
	second := first
	second.roles = []wire.RoleObserved{
		{Role: "prefill", Desired: 1, Ready: 0},
		{Role: "decode", Desired: 2, Ready: 2},
	}

	r.reportRollout(declared(t, "deployment", testDeploymentID, dep), dep, first)
	r.reportRollout(declared(t, "deployment", testDeploymentID, dep), dep, second)

	if len(rep.sent) != 2 {
		t.Fatalf("sent %d frames, want 2", len(rep.sent))
	}
	if !reflect.DeepEqual(rep.sent[0].Roles, first.roles) {
		t.Errorf("first roles = %+v, want %+v", rep.sent[0].Roles, first.roles)
	}
	if rep.sent[1].ReadyReplicas != 2 {
		t.Errorf("the scalars must not move: ready = %d, want 2", rep.sent[1].ReadyReplicas)
	}

	// And an identical restatement still says nothing new.
	r.reportRollout(declared(t, "deployment", testDeploymentID, dep), dep, second)
	if len(rep.sent) != 2 {
		t.Fatalf("an unchanged frame was re-sent: %d frames", len(rep.sent))
	}
}
