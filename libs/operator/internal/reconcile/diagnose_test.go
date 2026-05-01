package reconcile

import (
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
)

// component builds the object Dynamo derives from a graph deployment, carrying one condition.
func component(name, graph, status, reason, message string) *unstructured.Unstructured {
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(render.GVK.GroupVersion().WithKind("DynamoComponentDeployment"))
	obj.SetNamespace(testRender.Namespace)
	obj.SetName(name)
	obj.SetLabels(map[string]string{graphDeploymentLabel: graph})
	_ = unstructured.SetNestedSlice(obj.Object, []any{
		map[string]any{
			"type": "Available", "status": status, "reason": reason, "message": message,
		},
	}, "status", "conditions")
	return obj
}

func graphObject(name string, age time.Duration) *unstructured.Unstructured {
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(render.GVK)
	obj.SetNamespace(testRender.Namespace)
	obj.SetName(name)
	obj.SetCreationTimestamp(metav1.NewTime(time.Now().Add(-age)))
	return obj
}

func diagnoser(t *testing.T, objs ...*unstructured.Unstructured) *SyncedResourceReconciler {
	t.Helper()
	scheme := testScheme(t)
	builder := fake.NewClientBuilder().WithScheme(scheme)
	for _, obj := range objs {
		builder = builder.WithObjects(obj)
	}
	return &SyncedResourceReconciler{
		Client: builder.Build(), Scheme: scheme, Log: slog.Default(), Render: testRender,
	}
}

// The regression in full. The API server refused the Deployment the Dynamo operator tried to
// create, that refusal was written on an object the plane never sees, and the deployment reported
// `placed · 0 of 1 replicas ready` with no pod and no explanation for as long as it existed.
func TestARefusedSpecIsReportedRatherThanCountedAsStarting(t *testing.T) {
	const graph = "nvidia-nemotron-3-5-lightning-30b-a3b-bf"
	refused := `Failed to reconcile DynamoComponentDeployment: failed to create or update the ` +
		`deployment: Deployment.apps "` + graph + `-vllmdecodeworker-bb8c5a8c" is invalid: ` +
		`spec.selector.matchLabels: must be no more than 63 bytes`

	r := diagnoser(t,
		graphObject(graph, time.Minute),
		component(graph+"-vllmdecodeworker-bb8c5a8c", graph, "False", "Reconciling", refused),
	)

	out := rollout{desired: 1}
	out.trouble = r.diagnose(context.Background(), graph, nil, out)

	if out.trouble == nil {
		t.Fatal("no diagnosis for a spec the API server refused")
	}
	if out.reason() != "ClusterRejected" {
		t.Errorf("reason = %q, want ClusterRejected", out.reason())
	}
	if out.phase() != phaseFailed {
		t.Errorf("phase = %q, want %q — a permanent refusal is not a deployment starting up",
			out.phase(), phaseFailed)
	}
	if !strings.Contains(out.message(), "63 bytes") {
		t.Errorf("the controller's own words did not survive: %s", out.message())
	}
	if !strings.Contains(out.message(), "0 of 1 replicas ready") {
		t.Errorf("the counts were replaced rather than added to: %s", out.message())
	}
	if err := out.trouble.lastError("now"); err == nil || err.Code != "ClusterRejected" {
		t.Errorf("last_error not set: %+v", err)
	}
}

// A controller that is merely still working must not be reported as a failure. `Reconciling` with
// a summary message is what every deployment looks like for its first seconds.
func TestAControllerStillWorkingIsNotADiagnosis(t *testing.T) {
	const graph = "chat-iad"
	r := diagnoser(t,
		graphObject(graph, time.Minute),
		component(graph+"-vllmdecodeworker-aaaaaaaa", graph, "False", "Reconciling",
			"Starting to reconcile DynamoComponentDeployment"),
	)

	out := rollout{desired: 1}
	if found := r.diagnose(context.Background(), graph, nil, out); found != nil {
		t.Fatalf("diagnosed a deployment that is coming up normally: %+v", found)
	}
}

func TestPodTroubleIsReadFromTheWaitingReasonNotThePhase(t *testing.T) {
	pod := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "chat-iad-abc", Namespace: testRender.Namespace},
		Status: corev1.PodStatus{
			// Pending is also what a pod pulling an image normally reports, which is why
			// the phase cannot be the thing this reads.
			Phase: corev1.PodPending,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: "main",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{
					Reason:  "ImagePullBackOff",
					Message: "Back-off pulling image \"nvcr.io/nvidia/vllm:nope\"",
				}},
			}},
		},
	}
	found := troubleFromPods([]corev1.Pod{pod})
	if found == nil || found.reason != "ImagePullFailed" {
		t.Fatalf("trouble = %+v, want ImagePullFailed", found)
	}
	if !found.fatal {
		t.Error("an image that does not exist does not start existing by waiting")
	}
}

func TestAnUnschedulablePodIsReportedAndIsNotFatal(t *testing.T) {
	pod := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "chat-iad-abc", Namespace: testRender.Namespace},
		Status: corev1.PodStatus{Conditions: []corev1.PodCondition{{
			Type:    corev1.PodScheduled,
			Status:  corev1.ConditionFalse,
			Reason:  corev1.PodReasonUnschedulable,
			Message: "0/3 nodes are available: 3 Insufficient nvidia.com/gpu",
		}}},
	}
	found := troubleFromPods([]corev1.Pod{pod})
	if found == nil || found.reason != "PodUnschedulable" {
		t.Fatalf("trouble = %+v, want PodUnschedulable", found)
	}
	if found.fatal {
		t.Error("a node joining makes this placeable; it is not a permanent failure")
	}
	out := rollout{desired: 1, trouble: found}
	if out.phase() != phasePlaced {
		t.Errorf("phase = %q, want %q for a shortfall that can still clear", out.phase(), phasePlaced)
	}
}

// Silence past the window is itself the diagnosis. Nothing is wrong anywhere and nothing is
// running, which read as `placed` forever.
func TestSilencePastTheWindowIsReportedAsAFailure(t *testing.T) {
	const graph = "chat-iad"
	r := diagnoser(t, graphObject(graph, stallAfter+time.Minute))

	out := rollout{desired: 1}
	out.trouble = r.diagnose(context.Background(), graph, nil, out)

	if out.trouble == nil || out.trouble.reason != "NoReplicasStarted" {
		t.Fatalf("trouble = %+v, want NoReplicasStarted", out.trouble)
	}
	if out.phase() != phaseFailed {
		t.Errorf("phase = %q, want %q", out.phase(), phaseFailed)
	}
	if !strings.Contains(out.message(), "no pod was ever created") {
		t.Errorf("the message does not say what is missing: %s", out.message())
	}
}

func TestInsideTheWindowSilenceIsStillStartingUp(t *testing.T) {
	const graph = "chat-iad"
	r := diagnoser(t, graphObject(graph, time.Minute))

	out := rollout{desired: 1}
	if found := r.diagnose(context.Background(), graph, nil, out); found != nil {
		t.Fatalf("a deployment one minute into its life was called failed: %+v", found)
	}
}

// A serving deployment is never diagnosed, however loudly some old condition still complains.
func TestNothingIsDiagnosedOnceEveryReplicaIsReady(t *testing.T) {
	const graph = "chat-iad"
	r := diagnoser(t,
		graphObject(graph, time.Hour),
		component(graph+"-vllmdecodeworker-aaaaaaaa", graph, "False", "Failed", "is invalid: something"),
	)

	out := rollout{desired: 1, ready: 1}
	if found := r.diagnose(context.Background(), graph, nil, out); found != nil {
		t.Fatalf("diagnosed a deployment that is serving: %+v", found)
	}
}
