package reconcile

import (
	"context"
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
	sent []wire.DeploymentObserved
}

func (r *recordingReporter) Send(_ syncpkg.MessageType, _ string, payload any) error {
	if obs, ok := payload.(wire.DeploymentObserved); ok {
		r.sent = append(r.sent, obs)
	}
	return nil
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

	if len(rep.sent) != 1 {
		t.Fatalf("sent %d frames, want 1", len(rep.sent))
	}
	got := rep.sent[0]
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

	if len(rep.sent) != 1 {
		t.Fatalf("sent %d frames over four passes, want 1", len(rep.sent))
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
	if len(rep.sent) != 2 {
		t.Fatalf("sent %d frames, want the conclusion restated after the resync", len(rep.sent))
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
