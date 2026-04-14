package reconcile

import (
	"context"
	"errors"
	"log/slog"
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
	sent          []wire.DeploymentObserved
	replicaFrames []wire.ReplicaObserved
}

func (r *recordingReporter) Send(_ syncpkg.MessageType, _ string, payload any) error {
	switch p := payload.(type) {
	case wire.DeploymentObserved:
		r.sent = append(r.sent, p)
	case wire.ReplicaObserved:
		r.replicaFrames = append(r.replicaFrames, p)
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
		ClusterID: "cluster-ap",
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
	if got.Reason != "RecipeNotDeclared" {
		t.Errorf("reason = %q, want RecipeNotDeclared", got.Reason)
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
	if got.ClusterID != "cluster-ap" {
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
	if rep.sent[0].Reason != "Accepted" || rep.sent[1].Reason != "RecipeNotDeclared" {
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

// An observed deployment is held rather than applied, and that too is a state worth reporting: the
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

	if got := readyCondition(t, out).Reason; got != "RecipeNotDeclared" {
		t.Errorf("reason = %q, want RecipeNotDeclared", got)
	}
	if result.RequeueAfter == 0 {
		t.Error("a deployment waiting on its recipe must still ask to be called again")
	}
}

func TestNameToIDInvertsResourceName(t *testing.T) {
	if got := nameToID(resourceName(testDeploymentID)); got != testDeploymentID {
		t.Errorf("nameToID(resourceName(%q)) = %q", testDeploymentID, got)
	}
}

// A session the hub could not resume is one where the dedupe's assumption — that a skipped frame
// was delivered — does not hold: the hub prunes what it had and expects it restated.
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
	if got := rep.sent[2].Reason; got != "RecipeNotDeclared" {
		t.Errorf("restated reason = %q, want RecipeNotDeclared", got)
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

// TestWithReporterIsAPassThroughForOrdinaryFrames — it exists for the Resync hook and must not
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

// TestAFailedResyncKeepsWhatWasSaid — forgetting after a resync the store refused would restate
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

// TestWithReporterOnANilReconcilerIsTheStoreItself — the operator can run without the reconciler,
// and wrapping nil must not introduce a handler that dereferences it.
func TestWithReporterOnANilReconcilerIsTheStoreItself(t *testing.T) {
	inner := &chainSpy{}
	if got := WithReporter(inner, nil); got != syncpkg.Handler(inner) {
		t.Fatal("wrapping a nil reconciler did not return the store unchanged")
	}
}

// TestForgetAllIsSafeToCallTwice — a hub that opens two fresh sessions in a row must not panic.
func TestForgetAllIsSafeToCallTwice(t *testing.T) {
	r := &SyncedResourceReconciler{reported: map[string]reportKey{"dep-x": {}}}
	r.ForgetAll()
	r.ForgetAll()
	if len(r.reported) != 0 {
		t.Error("ForgetAll left state behind")
	}
}
