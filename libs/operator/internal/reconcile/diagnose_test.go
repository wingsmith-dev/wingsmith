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

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/render"
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
		t.Errorf("phase = %q, want %q: a permanent refusal is not a deployment starting up",
			out.phase(), phaseFailed)
	}
	if !strings.Contains(out.message(), "63 bytes") {
		t.Errorf("the controller's own words did not survive: %s", out.message())
	}
	if !strings.Contains(out.message(), "0 of 1 replicas ready") {
		t.Errorf("the counts were replaced, not added to: %s", out.message())
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

// The crash the whole evening turned on, and the one line that would have ended it in seconds.
//
// `CrashLoopBackOff` carries the kubelet's account of the loop ("back-off restarting failed
// container"), which is identical for every crash-looping container ever and says nothing about
// the fault. The process's own last words named the argument. They reach `lastState` only because
// the renderer asks for `FallbackToLogsOnError`, and they reach the plane only because this reads
// them.
func TestACrashLoopReportsWhatTheContainerSaidAndNotJustThatItLooped(t *testing.T) {
	pod := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "glm-5-3-flash-vllmdecodeworker-abc", Namespace: testRender.Namespace},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: "main",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{
					Reason:  "CrashLoopBackOff",
					Message: "back-off 40s restarting failed container=main",
				}},
				LastTerminationState: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{
					ExitCode: 2,
					Message:  "__main__.py: error: unrecognized arguments: --tool-call-parser glm47",
				}},
			}},
		},
	}
	found := troubleFromPods([]corev1.Pod{pod})
	if found == nil || found.reason != "CrashLooping" {
		t.Fatalf("trouble = %+v, want CrashLooping", found)
	}
	if !strings.Contains(found.message, "--tool-call-parser glm47") {
		t.Errorf("message = %q, want the argument the engine refused", found.message)
	}
}

// A pod from a render that predates the policy, or a container the kubelet killed, has no last
// words, and must still be reported, not dropped for lacking them.
func TestACrashLoopWithNoCapturedOutputStillReports(t *testing.T) {
	pod := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "chat-iad-abc", Namespace: testRender.Namespace},
		Status: corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: "main",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{
					Reason:  "CrashLoopBackOff",
					Message: "back-off 40s restarting failed container=main",
				}},
			}},
		},
	}
	found := troubleFromPods([]corev1.Pod{pod})
	if found == nil || found.reason != "CrashLooping" {
		t.Fatalf("trouble = %+v, want CrashLooping", found)
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

// A worker that is up and loading weights past the window is loading, not stalled. Reporting it
// as NoReplicasStarted showed a deployment still reading its weights as failed.
func TestARunningPodPastTheWindowIsStillLoading(t *testing.T) {
	const graph = "chat-iad"
	r := diagnoser(t, graphObject(graph, stallAfter+time.Hour))
	pod := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "chat-iad-abc", Namespace: testRender.Namespace},
		Status:     corev1.PodStatus{Phase: corev1.PodRunning},
	}

	out := rollout{desired: 1}
	out.trouble = r.diagnose(context.Background(), graph, []corev1.Pod{pod}, out)

	if out.trouble != nil {
		t.Fatalf("trouble = %+v, want none while the pod is running", out.trouble)
	}
	if out.phase() != phasePlaced {
		t.Errorf("phase = %q, want %q", out.phase(), phasePlaced)
	}
}

// One replica loading must not hide another that never left Pending. The stall used to be
// cleared by any Running pod, so a second replica stuck with no reason was never reported.
func TestAStuckPodBesideALoadingOneIsStillReported(t *testing.T) {
	const graph = "chat-iad"
	r := diagnoser(t, graphObject(graph, stallAfter+time.Hour))
	old := metav1.NewTime(time.Now().Add(-stallAfter - time.Hour))
	loading := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "chat-iad-a", Namespace: testRender.Namespace, CreationTimestamp: old},
		Status:     corev1.PodStatus{Phase: corev1.PodRunning},
	}
	stuck := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "chat-iad-b", Namespace: testRender.Namespace, CreationTimestamp: old},
		Status:     corev1.PodStatus{Phase: corev1.PodPending},
	}

	out := rollout{desired: 2}
	out.trouble = r.diagnose(context.Background(), graph, []corev1.Pod{loading, stuck}, out)

	if out.trouble == nil || out.trouble.reason != "NoReplicasStarted" {
		t.Fatalf("trouble = %+v, want NoReplicasStarted", out.trouble)
	}
	if !strings.Contains(out.trouble.message, "chat-iad-b has been Pending") {
		t.Errorf("the message should name the stuck pod: %s", out.trouble.message)
	}
}

// A replica that is serving must not hide another stuck in Pending either. The deployment is
// degraded, not failed, so the stuck pod is named without failing it.
func TestAStuckPodBesideAReadyOneIsReportedWithoutFailingTheDeployment(t *testing.T) {
	const graph = "chat-iad"
	r := diagnoser(t, graphObject(graph, stallAfter+time.Hour))
	old := metav1.NewTime(time.Now().Add(-stallAfter - time.Hour))
	ready := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "chat-iad-a", Namespace: testRender.Namespace, CreationTimestamp: old},
		Status: corev1.PodStatus{
			Phase:      corev1.PodRunning,
			Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}},
		},
	}
	stuck := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "chat-iad-b", Namespace: testRender.Namespace, CreationTimestamp: old},
		Status:     corev1.PodStatus{Phase: corev1.PodPending},
	}

	out := rollout{desired: 2, ready: 1}
	out.trouble = r.diagnose(context.Background(), graph, []corev1.Pod{ready, stuck}, out)

	if out.trouble == nil {
		t.Fatal("a pod stuck in Pending beside a ready one went unreported")
	}
	if !strings.Contains(out.message(), "chat-iad-b has been Pending") {
		t.Errorf("the message should name the stuck pod: %s", out.message())
	}
	if out.phase() != phaseDegraded {
		t.Errorf("phase = %q, want %q while a replica serves", out.phase(), phaseDegraded)
	}

	recent := stuck
	recent.CreationTimestamp = metav1.NewTime(time.Now().Add(-time.Minute))
	if found := r.diagnose(context.Background(), graph, []corev1.Pod{ready, recent}, out); found != nil {
		t.Fatalf("a pod one minute old beside a ready one was called stalled: %+v", found)
	}
}

// A pod recreated recently has only been stuck since then, whatever the graph's age.
func TestARecentlyRecreatedPendingPodIsNotYetStalled(t *testing.T) {
	const graph = "chat-iad"
	r := diagnoser(t, graphObject(graph, stallAfter+time.Hour))
	pod := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: "chat-iad-b", Namespace: testRender.Namespace,
			CreationTimestamp: metav1.NewTime(time.Now().Add(-time.Minute)),
		},
		Status: corev1.PodStatus{Phase: corev1.PodPending},
	}
	out := rollout{desired: 1}
	if found := r.diagnose(context.Background(), graph, []corev1.Pod{pod}, out); found != nil {
		t.Fatalf("a pod one minute old was called stalled: %+v", found)
	}
}

// An OOM kill leaves no output to quote. The exit is the only trace, and it has to reach the
// message or the crash loop reads the same as any other.
func TestACrashLoopSaysHowTheContainerLastExited(t *testing.T) {
	pod := corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "chat-iad-abc", Namespace: testRender.Namespace},
		Status: corev1.PodStatus{
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: "main",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{
					Reason:  "CrashLoopBackOff",
					Message: "back-off 40s restarting failed container=main",
				}},
				LastTerminationState: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{
					Reason:   "OOMKilled",
					ExitCode: 137,
				}},
			}},
		},
	}
	found := troubleFromPods([]corev1.Pod{pod})
	if found == nil || found.reason != "CrashLooping" {
		t.Fatalf("trouble = %+v, want CrashLooping", found)
	}
	for _, want := range []string{"OOMKilled", "code 137", "memory limit"} {
		if !strings.Contains(found.message, want) {
			t.Errorf("message = %q, want it to mention %q", found.message, want)
		}
	}
}
