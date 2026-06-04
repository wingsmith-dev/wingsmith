package reconcile

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation/field"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/config"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

const (
	testDeploymentID = "dep_01j9x2gggggggggggggggggggg"
	testArtifactID   = "art_01j9x2eeeeeeeeeeeeeeeeeeee"
	testImage        = "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1"
	testDigest       = "svg_1cd3f0e2a7b94c5d8e6f0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f"
)

var testRender = render.Options{
	Namespace:      "dynamo",
	PoolLabelKey:   "hangar.io/pool",
	GPUPoolValue:   "gpu",
	ModelCachePVC:  "model-cache",
	CachePoolValue: "cache",
	CacheTaintKey:  "hangar.io/cache",
}

func declared(t *testing.T, kind, id string, payload any) *v1alpha1.SyncedResource {
	t.Helper()
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal %s: %v", kind, err)
	}
	spec := v1alpha1.SyncedResourceSpec{
		Kind:     kind,
		Revision: 1,
		Payload:  &apiextensionsv1.JSON{Raw: raw},
	}
	// These tests are about what a managed deployment converges to. Observed is the default
	// everywhere else and has its own test below, because the gate is the interesting part.
	if kind == "deployment" {
		spec.Authority = v1alpha1.AuthorityManaged
	}
	return &v1alpha1.SyncedResource{
		ObjectMeta: metav1.ObjectMeta{Name: render.Name(id), Namespace: "hangar-system"},
		Spec:       spec,
	}
}

func testDeploymentDesired() wire.DeploymentDesired {
	return wire.DeploymentDesired{
		DeploymentID:    testDeploymentID,
		ClusterID:       "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
		ProjectID:       "prj_01j9x2jjjjjjjjjjjjjjjjjjjj",
		MinReplicas:     2,
		MaxReplicas:     8,
		Generation:      14,
		ArtifactIDs:     []string{testArtifactID},
		ServedModelName: "qwen3.8-27b",
		Placement:       wire.PlacementConstraints{DeviceClass: "h100-80gb-sxm", DevicesPerReplica: 2},
		Serving:         testServingSpec(),
	}
}

func testServingSpec() wire.ServingSpec {
	image := testImage
	return wire.ServingSpec{
		Engine:        "vllm",
		EngineImage:   &image,
		Parallelism:   wire.Parallelism{Tensor: 2, Pipeline: 1, Expert: 1},
		ContextLength: 262144,
		Digest:        testDigest,
	}
}

func testArtifactDesired() wire.ArtifactDesired {
	return wire.ArtifactDesired{
		ArtifactID: testArtifactID,
		Kind:       "weights",
		Digest:     "sha256:9f2c1b7a",
		Sources:    []wire.ArtifactSource{{Kind: "hf", URI: "hf://Qwen/Qwen3.8-27B", Priority: 0}},
	}
}

func gpuNode(name string, devices int64, ready bool) *corev1.Node {
	condition := corev1.ConditionFalse
	if ready {
		condition = corev1.ConditionTrue
	}
	return &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name, Labels: map[string]string{"hangar.io/pool": "gpu"}},
		Status: corev1.NodeStatus{
			Allocatable: corev1.ResourceList{"nvidia.com/gpu": *resource.NewQuantity(devices, resource.DecimalSI)},
			Conditions:  []corev1.NodeCondition{{Type: corev1.NodeReady, Status: condition}},
		},
	}
}

// completedPrefetch is the download this cluster has already finished, labelled the way the render
// labels it — which is how every lookup finds it, the name being only a rendering.
func completedPrefetch() *batchv1.Job {
	art := testArtifactDesired()
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      render.PrefetchJobName(art),
			Namespace: "dynamo",
			Labels:    map[string]string{render.ArtifactLabel: art.ArtifactID},
		},
		Status: batchv1.JobStatus{Succeeded: 1},
	}
}

// frontendPods builds n pods of a Frontend component sharing the worker's graph, labelled the way
// the Dynamo operator labels every pod it derives from one graph.
func frontendPods(n int) []*corev1.Pod {
	out := make([]*corev1.Pod, 0, n)
	for i := 0; i < n; i++ {
		out = append(out, &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      fmt.Sprintf("%s-frontend-def-%d", render.Name(testDeploymentID), i),
				Namespace: "dynamo",
				Labels: map[string]string{
					graphDeploymentLabel: render.Name(testDeploymentID),
					componentNameLabel:   "Frontend",
					"pod-template-hash":  "def",
				},
			},
			Spec: corev1.PodSpec{NodeName: "cpu-0"},
			Status: corev1.PodStatus{
				Phase:      corev1.PodRunning,
				Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}},
			},
		})
	}
	return out
}

// readyPods builds n pods labelled the way the Dynamo operator labels the ones it creates.
func readyPods(n int) []*corev1.Pod {
	out := make([]*corev1.Pod, 0, n)
	for i := 0; i < n; i++ {
		out = append(out, &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      fmt.Sprintf("%s-vllmdecodeworker-abc-%d", render.Name(testDeploymentID), i),
				Namespace: "dynamo",
				Labels: map[string]string{
					graphDeploymentLabel: render.Name(testDeploymentID),
					componentNameLabel:   render.WorkerComponent,
					"pod-template-hash":  "abc",
				},
			},
			Spec: corev1.PodSpec{NodeName: "gpu-0"},
			Status: corev1.PodStatus{
				Phase:      corev1.PodRunning,
				Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}},
			},
		})
	}
	return out
}

func runReconcile(t *testing.T, objects ...client.Object) (*SyncedResourceReconciler, *v1alpha1.SyncedResource, ctrl.Result) {
	return runReconcileWith(t, nil, objects...)
}

func runReconcileWith(t *testing.T, reporter Reporter, objects ...client.Object) (*SyncedResourceReconciler, *v1alpha1.SyncedResource, ctrl.Result) {
	t.Helper()
	return runReconcileAs(t, reporter, nil, objects...)
}

// runReconcileCapable reconciles against an observation loop that has reported a capability.
func runReconcileCapable(t *testing.T, caps Capabilities, objects ...client.Object) (*SyncedResourceReconciler, *v1alpha1.SyncedResource, ctrl.Result) {
	t.Helper()
	return runReconcileAs(t, nil, caps, objects...)
}

func runReconcileAs(t *testing.T, reporter Reporter, caps Capabilities, objects ...client.Object) (*SyncedResourceReconciler, *v1alpha1.SyncedResource, ctrl.Result) {
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
		ClusterID: "clu_01j9x2aaaaaaaaaaaaaaaaaaaa", Report: reporter,
		Capabilities: caps,
	}

	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: subject.GetNamespace(), Name: subject.GetName()}}
	result, err := r.Reconcile(context.Background(), req)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var out v1alpha1.SyncedResource
	if err := c.Get(context.Background(), req.NamespacedName, &out); err != nil {
		t.Fatalf("read back: %v", err)
	}
	return r, &out, result
}

func readyCondition(t *testing.T, res *v1alpha1.SyncedResource) metav1.Condition {
	t.Helper()
	for _, c := range res.Status.Conditions {
		if c.Type == "Ready" {
			return c
		}
	}
	t.Fatalf("no Ready condition on %s", res.Name)
	return metav1.Condition{}
}

func TestDeploymentWaitsForItsArtifact(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	_, out, result := runReconcile(t, dep)

	if got := readyCondition(t, out).Reason; got != "ArtifactNotDeclared" {
		t.Errorf("reason = %q, want ArtifactNotDeclared", got)
	}
	if result.RequeueAfter == 0 {
		t.Error("a deployment waiting on its artifact must ask to be called again")
	}
	if out.Status.ObservedRevision != 0 {
		t.Error("nothing was applied, so the revision must not be marked observed")
	}
}

// The gate that matters. Several replicas resolving a cold shared cache race on lock files that
// do not hold over NFS, and the loser crash-loops the whole graph deployment.
func TestDeploymentWaitsForResidentWeights(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art)

	if got := readyCondition(t, out).Reason; got != "WeightsNotResident" {
		t.Errorf("reason = %q, want WeightsNotResident", got)
	}

	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)
	key := types.NamespacedName{Namespace: "dynamo", Name: render.Name(testDeploymentID)}
	if err := r.Client.Get(context.Background(), key, graph); err == nil {
		t.Fatal("workers were rendered against a cold cache")
	}
}

func TestDeploymentRendersOnceWeightsAreResident(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	condition := readyCondition(t, out)
	if condition.Status != metav1.ConditionTrue {
		t.Fatalf("Ready = %s (%s: %s)", condition.Status, condition.Reason, condition.Message)
	}
	if out.Status.ObservedRevision != out.Spec.Revision {
		t.Errorf("observed revision = %d, want %d", out.Status.ObservedRevision, out.Spec.Revision)
	}

	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)
	key := types.NamespacedName{Namespace: "dynamo", Name: render.Name(testDeploymentID)}
	if err := r.Client.Get(context.Background(), key, graph); err != nil {
		t.Fatalf("graph deployment was not applied: %v", err)
	}

	// The declared copy is in hangar-system and this is in dynamo, so an owner reference here is
	// not a link — Kubernetes resolves one within the dependent's own namespace, finds nothing,
	// and deletes the object it is stamped on. This is the regression that made a deployment
	// report Applied and then quietly not exist.
	if owners := graph.GetOwnerReferences(); len(owners) != 0 {
		t.Errorf("owner references = %v, want none across a namespace boundary", owners)
	}
	labels := graph.GetLabels()
	if got := labels[render.OwnerNameLabel]; got != render.Name(testDeploymentID) {
		t.Errorf("%s = %q, want %q", render.OwnerNameLabel, got, render.Name(testDeploymentID))
	}
	if got := labels[render.OwnerNamespaceLabel]; got != "hangar-system" {
		t.Errorf("%s = %q, want hangar-system", render.OwnerNamespaceLabel, got)
	}

	// Nothing else will delete it now, so the declared copy has to be held open until this
	// operator has.
	if !controllerutil.ContainsFinalizer(out, renderedFinalizer) {
		t.Errorf("finalizers = %v, want %s", out.Finalizers, renderedFinalizer)
	}
}

// A rendered object can go away without desired state changing — someone deletes it, or an
// earlier version of this operator left one that the garbage collector took. The watch is the
// fast path back; this is the floor under it.
func TestAFullyServingDeploymentIsLookedAtAgainWithoutBeingWrittenTo(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	objects := []client.Object{dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true)}
	for _, pod := range readyPods(2) {
		objects = append(objects, pod)
	}
	_, _, result := runReconcile(t, objects...)

	if result.RequeueAfter != resyncAfterApply {
		t.Errorf("RequeueAfter = %s, want the drift floor %s", result.RequeueAfter, resyncAfterApply)
	}
}

// A rollout that has not finished keeps a short polling floor under the pod watch. A missed event,
// a restarting watch, or a pod that has not been created yet must not leave progress stale for the
// full drift interval.
func TestARolloutStillFillingInIsLookedAtSoon(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, _, result := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	if result.RequeueAfter != waitForDependency {
		t.Errorf("RequeueAfter = %s, want %s while nothing is ready yet", result.RequeueAfter, waitForDependency)
	}
}

// What is running is reported, not what was applied. This is the difference between a console
// that says a deployment is serving and one that says `placed · 0/0` while it serves.
func TestWhatIsRunningIsReportedAndNotOnlyWhatWasApplied(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	objects := []client.Object{dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true)}
	for _, pod := range readyPods(2) {
		objects = append(objects, pod)
	}

	reporter := &recordingReporter{}
	runReconcileReporting(t, reporter, objects...)

	last := lastSent(t, reporter)
	if last.Phase != "running" {
		t.Errorf("phase = %q, want running", last.Phase)
	}
	if last.ReadyReplicas != 2 || last.DesiredReplicas != 2 {
		t.Errorf("replicas = %d/%d, want 2/2", last.ReadyReplicas, last.DesiredReplicas)
	}
	if got := len(reporter.replicaFrames); got != 2 {
		t.Errorf("replica frames = %d, want one per pod", got)
	}
}

// Kubernetes' pod phases and the contract's replica phases overlap enough to be mistaken for each
// other and disagree on the two that matter. Emitting the Kubernetes spelling had the plane reject
// every replica frame as an invalid enum value, which is indistinguishable — from the console —
// from a deployment with no replicas at all.
func TestAReplicaPhaseIsAlwaysOneTheContractDefines(t *testing.T) {
	// MESSAGES.md, and the `Literal` the plane decodes against.
	allowed := map[string]bool{
		"pending": true, "starting": true, "ready": true,
		"draining": true, "terminated": true, "failed": true,
	}
	cases := []struct {
		phase corev1.PodPhase
		ready bool
		want  string
	}{
		{corev1.PodPending, false, "pending"},
		{corev1.PodRunning, false, "starting"},
		{corev1.PodRunning, true, "ready"},
		{corev1.PodSucceeded, false, "terminated"},
		{corev1.PodFailed, false, "failed"},
		{corev1.PodUnknown, false, "pending"},
	}
	for _, c := range cases {
		pod := &corev1.Pod{Status: corev1.PodStatus{Phase: c.phase}}
		got := replicaPhase(pod, c.ready)
		if !allowed[got] {
			t.Errorf("pod %s ready=%v -> %q, which the contract does not define", c.phase, c.ready, got)
		}
		if got != c.want {
			t.Errorf("pod %s ready=%v -> %q, want %q", c.phase, c.ready, got, c.want)
		}
	}
}

// A nil slice marshals to `null`, and `device_ids` is required on the wire — so every replica
// frame this loop sent was rejected by the plane as invalid and the replica rollup sat at zero
// for deployments that were plainly serving. The encoding is asserted rather than the field,
// because the field being non-nil is only interesting for what JSON it produces.
func TestAReplicaFrameCarriesDeviceIDsAsAnArrayAndNeverNull(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	objects := []client.Object{dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true)}
	for _, pod := range readyPods(1) {
		objects = append(objects, pod)
	}

	reporter := &recordingReporter{}
	runReconcileReporting(t, reporter, objects...)

	if len(reporter.replicaFrames) == 0 {
		t.Fatal("no replica frame was reported")
	}
	encoded, err := json.Marshal(reporter.replicaFrames[0])
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !strings.Contains(string(encoded), `"device_ids":[]`) {
		t.Errorf("device_ids did not encode as an empty array: %s", encoded)
	}
}

// A shortfall while something is still serving is degraded, which is what a rolling update and a
// crash-looping replica both look like.
func TestAPartialRolloutIsDegradedRatherThanRunning(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	objects := []client.Object{dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true)}
	for _, pod := range readyPods(1) {
		objects = append(objects, pod)
	}

	reporter := &recordingReporter{}
	runReconcileReporting(t, reporter, objects...)

	if got := lastSent(t, reporter).Phase; got != "degraded" {
		t.Errorf("phase = %q, want degraded with 1 of 2 ready", got)
	}
}

// The suppression rule has to survive there being two things that report a deployment.
//
// The regression: the reconcile conclusion and the read-back rollout carry different replica
// counts, so when both were sent each looked like news against the other's remembered key and the
// pair re-sent on every pass — forever, for every deployment in the fleet.
func TestAServingDeploymentIsNotReportedAgainOnAnUnchangedPass(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	objects := []client.Object{dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true)}
	for _, pod := range readyPods(2) {
		objects = append(objects, pod)
	}

	reporter := &recordingReporter{}
	r, _ := runReconcileReporting(t, reporter, objects...)
	first := len(reporter.sent)

	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "hangar-system", Name: dep.Name}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("second pass: %v", err)
	}

	if len(reporter.sent) != first {
		t.Errorf("sent %d frames after an unchanged second pass, want %d", len(reporter.sent), first)
	}
}

// A pod that is Running but not Ready is starting, and for an inference worker that is most of
// the wait — the container is up while tens of gigabytes load into VRAM.
func TestAWorkerLoadingWeightsIsStartingRatherThanRunning(t *testing.T) {
	pod := readyPods(1)[0]
	pod.Status.Conditions = []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionFalse}}

	if got := replicaPhase(pod, false); got != "starting" {
		t.Errorf("phase = %q, want starting", got)
	}
}

// Teardown is a code path rather than the API server's job, because the owner reference that used
// to do it cannot cross the namespace boundary this render puts the two objects on.
func TestDeletingADeclaredDeploymentRemovesWhatItRendered(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	ctx := context.Background()
	key := types.NamespacedName{Namespace: "dynamo", Name: render.Name(testDeploymentID)}
	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)
	if err := r.Client.Get(ctx, key, graph); err != nil {
		t.Fatalf("nothing was rendered to tear down: %v", err)
	}

	if err := r.Client.Delete(ctx, out); err != nil {
		t.Fatalf("delete declared copy: %v", err)
	}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: out.Namespace, Name: out.Name}}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("reconcile after delete: %v", err)
	}

	if err := r.Client.Get(ctx, key, graph); !apierrors.IsNotFound(err) {
		t.Errorf("graph deployment survived its declared copy: %v", err)
	}
	var left v1alpha1.SyncedResource
	err := r.Client.Get(ctx, types.NamespacedName{Namespace: out.Namespace, Name: out.Name}, &left)
	if !apierrors.IsNotFound(err) {
		t.Errorf("declared copy still held by a finalizer: %v (%v)", err, left.Finalizers)
	}
}

// A rename renders under a new object name, and nothing else would ever collect what the old
// name created: the declared copy is not being deleted, so teardown never runs, and the apply
// only touches the name it was given.
func TestRenamingADeploymentRemovesWhatTheOldNameRendered(t *testing.T) {
	desired := testDeploymentDesired()
	desired.Name = "chat-iad"
	dep := declared(t, "deployment", testDeploymentID, desired)
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	ctx := context.Background()
	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)
	old := types.NamespacedName{Namespace: "dynamo", Name: "chat-iad"}
	if err := r.Client.Get(ctx, old, graph); err != nil {
		t.Fatalf("nothing rendered under the first name: %v", err)
	}

	desired.Name = "chat-iad-2"
	desired.Generation++
	raw, err := json.Marshal(desired)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	out.Spec.Payload = &apiextensionsv1.JSON{Raw: raw}
	out.Spec.Revision = 2
	if err := r.Client.Update(ctx, out); err != nil {
		t.Fatalf("update declared copy: %v", err)
	}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: out.Namespace, Name: out.Name}}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("reconcile after rename: %v", err)
	}

	if err := r.Client.Get(ctx, types.NamespacedName{Namespace: "dynamo", Name: "chat-iad-2"}, graph); err != nil {
		t.Fatalf("nothing rendered under the new name: %v", err)
	}
	if err := r.Client.Get(ctx, old, graph); !apierrors.IsNotFound(err) {
		t.Errorf("the old name is still serving: %v", err)
	}
}

func TestDeploymentFailsWithoutRetryOnAnUndecodablePayload(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	dep.Spec.Payload = &apiextensionsv1.JSON{Raw: []byte(`{"min_replicas":"two"}`)}
	_, out, result := runReconcile(t, dep)

	if out.Status.Phase != "Failed" {
		t.Errorf("phase = %q, want Failed", out.Status.Phase)
	}
	if result.RequeueAfter != 0 || result.Requeue {
		t.Error("a payload that does not decode is not fixed by retrying it")
	}
}

func TestUnknownKindIsReportedRatherThanIgnored(t *testing.T) {
	res := declared(t, "sandwich", "snd_01j9x2", map[string]string{})
	_, out, _ := runReconcile(t, res)

	if got := readyCondition(t, out).Reason; got != "UnknownKind" {
		t.Errorf("reason = %q, want UnknownKind", got)
	}
}

func TestAnArtifactIsHeldWithoutClusterWork(t *testing.T) {
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, art)

	if out.Status.Phase != "Accepted" {
		t.Errorf("phase = %q, want Accepted", out.Status.Phase)
	}
	if out.Status.ObservedRevision != out.Spec.Revision {
		t.Error("a declared object with nothing to apply is still observed at its revision")
	}
}

// A cluster whose nodes advertise no accelerators is indistinguishable, from the scheduler's side,
// from a cluster where the device plugin was never installed. Both must be said out loud rather
// than left as pods pending forever.
func TestDeploymentWaitsWhenNoNodeCanHoldAReplica(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 0, true))

	condition := readyCondition(t, out)
	if condition.Reason != "NoSchedulableCapacity" {
		t.Errorf("reason = %q, want NoSchedulableCapacity", condition.Reason)
	}
	if !strings.Contains(condition.Message, "device plugin") {
		t.Errorf("message should name the likeliest cause, got %q", condition.Message)
	}
}

// Capacity spread thinly over several nodes does not satisfy a tensor group, which is the unit
// that has to fit inside one machine. Two devices at tensor-parallel two are one such group.
func TestCapacityIsCountedPerNodeNotClusterWide(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, art, completedPrefetch(),
		gpuNode("gpu-0", 1, true), gpuNode("gpu-1", 1, true))

	if got := readyCondition(t, out).Reason; got != "NoSchedulableCapacity" {
		t.Errorf("reason = %q, want NoSchedulableCapacity: two devices on two nodes cannot run a two-device replica", got)
	}
}

// A drain in progress is when this matters most: the accelerators are still allocatable and still
// reported, and counting them would promise capacity that is deliberately being removed.
func TestCordonedNodesDoNotCountAsCapacity(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	draining := gpuNode("gpu-0", 8, true)
	draining.Spec.Unschedulable = true
	_, out, _ := runReconcile(t, dep, art, completedPrefetch(), draining)

	if got := readyCondition(t, out).Reason; got != "NoSchedulableCapacity" {
		t.Errorf("reason = %q, want NoSchedulableCapacity", got)
	}
}

func TestNodesOutsideThePlacementSelectorAreNotCounted(t *testing.T) {
	desired := testDeploymentDesired()
	desired.Placement.NodeSelector = map[string]string{"region": "eu-west-1"}
	dep := declared(t, "deployment", testDeploymentID, desired)
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	if got := readyCondition(t, out).Reason; got != "NoSchedulableCapacity" {
		t.Errorf("reason = %q, want NoSchedulableCapacity: the only usable node is in another region", got)
	}
}

// TestObservedDeploymentIsNeverApplied is the gate ADR 0009 turns on. Everything a managed
// deployment needs in order to render is present here — artifact, resident weights, a node with
// room — so if anything is written it is the authority mark that failed and not a missing input.
func TestObservedDeploymentIsNeverApplied(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	dep.Spec.Authority = v1alpha1.AuthorityObserved
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)
	key := types.NamespacedName{Namespace: "dynamo", Name: render.Name(testDeploymentID)}
	if err := r.Client.Get(context.Background(), key, graph); err == nil {
		t.Fatal("an observed deployment was rendered into the cluster")
	}
	if out.Status.Phase == "Failed" {
		t.Errorf("declining to act is not a failure, got phase %q", out.Status.Phase)
	}
}

// TestAnEmptyAuthorityIsObserved covers the upgrade path: desired state written by a plane that
// predates the field must not acquire management by omission.
func TestAnEmptyAuthorityIsObserved(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	dep.Spec.Authority = ""
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, _, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)
	key := types.NamespacedName{Namespace: "dynamo", Name: render.Name(testDeploymentID)}
	if err := r.Client.Get(context.Background(), key, graph); err == nil {
		t.Fatal("an unmarked deployment was rendered into the cluster")
	}
}

// legacyRendered is a graph deployment rendered under the id-derived name, from before this
// operator stamped the deployment-id label on what it rendered.
func legacyRendered(t *testing.T, name string, labels map[string]string) *unstructured.Unstructured {
	t.Helper()
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(render.GVK)
	obj.SetNamespace(testRender.Namespace)
	obj.SetName(name)
	if labels != nil {
		obj.SetLabels(labels)
	}
	return obj
}

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := clientgoscheme.Scheme
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	return scheme
}

func rendered(t *testing.T, r *SyncedResourceReconciler, name string) bool {
	t.Helper()
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(render.GVK)
	err := r.Client.Get(context.Background(), types.NamespacedName{
		Namespace: testRender.Namespace, Name: name,
	}, obj)
	if err != nil && !apierrors.IsNotFound(err) {
		t.Fatalf("get %s: %v", name, err)
	}
	return err == nil
}

// TestARenamedWorkloadFromBeforeTheLabelIsCollected: the label is how a rendered object is found,
// so one rendered before the label existed is invisible to the prune and nothing else would ever
// collect it. In production one outlived a rename by days, holding a GPU and serving a duplicate
// of the live model into the same namespace.
func TestARenamedWorkloadFromBeforeTheLabelIsCollected(t *testing.T) {
	legacy := legacyRendered(t, render.Name(testDeploymentID), nil)
	r := &SyncedResourceReconciler{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(legacy).Build(),
		Log:    slog.Default(), Render: testRender, ClusterID: "ap-cluster-1",
	}

	if err := r.pruneRendered(context.Background(), testDeploymentID, "gpt-oss-20b"); err != nil {
		t.Fatalf("prune: %v", err)
	}
	if rendered(t, r, render.Name(testDeploymentID)) {
		t.Fatal("the unlabelled object survived; nothing else will ever collect it")
	}
}

// TestTheCurrentRenderIsNeverCollectedByName: a deployment with no renderable name renders under
// exactly the id-derived name, and collecting it would delete the workload on every pass.
func TestTheCurrentRenderIsNeverCollectedByName(t *testing.T) {
	name := render.Name(testDeploymentID)
	current := legacyRendered(t, name, nil)
	r := &SyncedResourceReconciler{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(current).Build(),
		Log:    slog.Default(), Render: testRender, ClusterID: "ap-cluster-1",
	}

	if err := r.pruneRendered(context.Background(), testDeploymentID, name); err != nil {
		t.Fatalf("prune: %v", err)
	}
	if !rendered(t, r, name) {
		t.Fatal("the object being kept was deleted")
	}
}

// TestAnObjectLabelledForAnotherDeploymentIsLeftAlone: a label that disagrees is evidence, and the
// only reading of it is that the object is somebody else's.
func TestAnObjectLabelledForAnotherDeploymentIsLeftAlone(t *testing.T) {
	name := render.Name(testDeploymentID)
	theirs := legacyRendered(t, name, map[string]string{deploymentIDLabel: "dep_01someoneelse"})
	r := &SyncedResourceReconciler{
		Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(theirs).Build(),
		Log:    slog.Default(), Render: testRender, ClusterID: "ap-cluster-1",
	}

	if err := r.pruneRendered(context.Background(), testDeploymentID, "gpt-oss-20b"); err != nil {
		t.Fatalf("prune: %v", err)
	}
	if !rendered(t, r, name) {
		t.Fatal("an object belonging to another deployment was deleted")
	}
}

// A frontend is not a replica of the model it fronts.
//
// Dynamo stamps the graph label on every pod it derives, so a graph holding a frontend beside its
// worker offers both to the same lookup. Counting the frontend would report a deployment as
// serving the moment two pods holding no weights went ready — while the worker is still forty
// minutes from its first token — and the plane marks a deployment ready off exactly this count.
func TestFrontendPodsAreNotCountedAsReplicasOfTheModel(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	objects := []client.Object{dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true)}
	for _, pod := range readyPods(1) {
		objects = append(objects, pod)
	}
	for _, pod := range frontendPods(2) {
		objects = append(objects, pod)
	}

	reporter := &recordingReporter{}
	runReconcileReporting(t, reporter, objects...)

	last := lastSent(t, reporter)
	if last.ReadyReplicas != 1 {
		t.Errorf("ready = %d, want the one worker and neither frontend", last.ReadyReplicas)
	}
	if last.Phase != "degraded" {
		t.Errorf("phase = %q, want degraded while one of two workers is up", last.Phase)
	}
	if len(last.ReplicaIDs) != 1 {
		t.Errorf("reported %d replica ids, want only the worker's", len(last.ReplicaIDs))
	}
}

// A per-cluster setting nobody carries into the render is a setting that does nothing, and the
// frontend settings fail that way silently: the graph renders, the workers come up, and the
// frontend is either absent or wearing this cluster's defaults instead of its own.
func TestRenderOptionsCarriesTheFrontendSettings(t *testing.T) {
	cfg := &config.Config{
		WorkloadNamespace:              "dynamo",
		PoolLabelKey:                   "hangar.io/pool",
		EnginePullSecrets:              []string{"ghcr-engine"},
		FrontendEnabled:                true,
		FrontendImage:                  "ghcr.io/hangar-dev/vllm-runtime:1.4.0-patched.1-hgr2",
		FrontendReplicas:               3,
		FrontendPullSecrets:            []string{"ghcr-hangar"},
		FrontendCPU:                    "500m",
		FrontendMemory:                 "1Gi",
		FrontendMemoryLimit:            "4Gi",
		FrontendAnthropicAPI:           true,
		FrontendAnthropicStripPreamble: true,
		FrontendGlobalDynamoNamespace:  false,
	}
	opts := RenderOptions(cfg)

	if !opts.FrontendEnabled || opts.FrontendImage != cfg.FrontendImage || opts.FrontendReplicas != 3 {
		t.Fatalf("frontend options not carried: %+v", opts)
	}
	if len(opts.FrontendPullSecrets) != 1 || opts.FrontendPullSecrets[0] != "ghcr-hangar" {
		t.Fatalf("frontend pull secrets = %v", opts.FrontendPullSecrets)
	}
	if len(opts.EnginePullSecrets) != 1 || opts.EnginePullSecrets[0] != "ghcr-engine" {
		t.Fatalf("engine pull secrets = %v", opts.EnginePullSecrets)
	}
	if opts.FrontendCPU != "500m" || opts.FrontendMemory != "1Gi" || opts.FrontendMemoryLimit != "4Gi" {
		t.Fatalf("frontend resources = %q %q %q", opts.FrontendCPU, opts.FrontendMemory, opts.FrontendMemoryLimit)
	}
	if !opts.AnthropicAPIEnabled || !opts.AnthropicStripPreamble {
		t.Fatalf("anthropic switches = %v %v", opts.AnthropicAPIEnabled, opts.AnthropicStripPreamble)
	}
	// A pointer, because nil means the shared namespace every cluster runs today. A cluster
	// that has said false must not read as one that said nothing.
	if opts.FrontendGlobalNamespace == nil || *opts.FrontendGlobalNamespace {
		t.Fatalf("global dynamo namespace = %v", opts.FrontendGlobalNamespace)
	}
}

// ------------------------------------------------- the frontend a cluster renders per model

// stuckFrontendPods builds n frontend pods the kubelet has given up pulling, which is what a
// private frontend image looks like when the workload namespace holds no Secret for it.
func stuckFrontendPods(n int) []*corev1.Pod {
	out := frontendPods(n)
	for _, pod := range out {
		pod.Status = corev1.PodStatus{
			Phase: corev1.PodPending,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: "main",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{
					Reason:  "ImagePullBackOff",
					Message: `Back-off pulling image "ghcr.io/hangar-dev/vllm-runtime:1.4.0-patched.1-hgr2"`,
				}},
			}},
		}
	}
	return out
}

// startingFrontendPods builds n frontend pods that are simply coming up, which every rollout
// passes through and which nothing should call a failure.
func startingFrontendPods(n int) []*corev1.Pod {
	out := frontendPods(n)
	for _, pod := range out {
		pod.Status = corev1.PodStatus{
			Phase: corev1.PodPending,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:  "main",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "ContainerCreating"}},
			}},
		}
	}
	return out
}

// observedRollout reads one deployment's rollout against the pods given, under the render options
// given. `observeRollout` rather than a whole reconcile, because the render options are the
// variable under test and the reconcile helpers share one set of them.
func observedRollout(t *testing.T, opts render.Options, pods ...*corev1.Pod) rollout {
	t.Helper()
	objects := make([]client.Object, 0, len(pods))
	for _, pod := range pods {
		objects = append(objects, pod)
	}
	r := &SyncedResourceReconciler{
		Client:    fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithObjects(objects...).Build(),
		Scheme:    clientgoscheme.Scheme,
		Log:       slog.Default(),
		Namespace: "hangar-system",
		Render:    opts,
		ClusterID: "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
	}
	out, err := r.observeRollout(context.Background(), testDeploymentDesired())
	if err != nil {
		t.Fatalf("observe: %v", err)
	}
	return out
}

func frontendRender() render.Options {
	opts := testRender
	opts.FrontendEnabled = true
	return opts
}

// The failure one frontend per model introduces, and the one nothing else here would report. The
// workers are all ready, so `diagnose` is never called; without this the plane is told
// `running · Serving` for a deployment whose only way in never started, the gateway prefers it,
// and every request 502s. It is the same shape as the overlay that deletes the frontend, which
// the renderer refuses for the same reason.
func TestAFrontendThatCannotStartIsNotAServingDeployment(t *testing.T) {
	pods := append(readyPods(2), stuckFrontendPods(2)...)
	out := observedRollout(t, frontendRender(), pods...)

	if out.ready != 2 || out.desired != 2 {
		t.Fatalf("counts = %d of %d; the frontend must not move the model's replica counts", out.ready, out.desired)
	}
	if out.phase() != phaseFailed {
		t.Errorf("phase = %q, want failed while nothing can reach the model", out.phase())
	}
	if out.reason() != "ImagePullFailed" {
		t.Errorf("reason = %q, want the pod's own waiting reason", out.reason())
	}
	if !strings.Contains(out.message(), "no frontend is serving") {
		t.Errorf("message does not say the frontend is the part that failed: %q", out.message())
	}
}

// A cluster that has not opted in is served by the one frontend the chart installs, which belongs
// to no deployment and is not in this graph. Reading a shortfall there would report a failure
// against a frontend that was never this deployment's — so with the option off, nothing is read
// and the counts are the whole answer, exactly as before.
func TestTheFrontendIsNotReadUntilTheClusterOptsIn(t *testing.T) {
	pods := append(readyPods(2), stuckFrontendPods(2)...)
	out := observedRollout(t, testRender, pods...)

	if out.phase() != phaseRunning || out.reason() != "Serving" {
		t.Errorf("phase = %q, reason = %q; a cluster that has not moved must read as it always did",
			out.phase(), out.reason())
	}
}

// The ordinary case, and the one that must not become noisy: a frontend that is up leaves the
// deployment saying exactly what its replica counts say.
func TestAReadyFrontendLeavesTheDeploymentServing(t *testing.T) {
	pods := append(readyPods(2), frontendPods(2)...)
	out := observedRollout(t, frontendRender(), pods...)

	if out.phase() != phaseRunning || out.reason() != "Serving" {
		t.Errorf("phase = %q, reason = %q", out.phase(), out.reason())
	}
}

// A frontend nobody can place is not fatal — a node joining fixes it with nothing about the
// deployment having changed — but it is still a model nothing can reach, so the deployment must
// not read `running` for as long as it lasts. This is the case the counts alone get wrong in the
// other direction: every worker ready, and degraded is the honest word.
func TestAFrontendNobodyCanPlaceLeavesTheDeploymentDegraded(t *testing.T) {
	frontends := frontendPods(1)
	frontends[0].Status = corev1.PodStatus{
		Phase: corev1.PodPending,
		Conditions: []corev1.PodCondition{{
			Type:    corev1.PodScheduled,
			Status:  corev1.ConditionFalse,
			Reason:  corev1.PodReasonUnschedulable,
			Message: "0/3 nodes are available: 3 Insufficient cpu",
		}},
	}
	out := observedRollout(t, frontendRender(), append(readyPods(2), frontends...)...)

	if out.phase() != phaseDegraded {
		t.Errorf("phase = %q, want degraded: every worker is ready and no request can reach one", out.phase())
	}
	if out.reason() != "PodUnschedulable" {
		t.Errorf("reason = %q", out.reason())
	}
}

// A frontend still coming up is a shortfall and not a diagnosis, and the two answers are kept
// apart on purpose.
//
// `degraded`, because until a frontend is ready nothing can reach the model however many workers
// are — and `degraded` is what a rolling update passes through, not a failure. It costs nothing
// to say so: the gateway orders candidates by readiness and never removes the last one, so a
// deployment with one upstream still takes traffic while it says this, and one with two prefers
// the upstream that can actually answer.
//
// `Progressing` and not a reason, because a pod that is merely creating its container has said
// nothing, and neither has a graph Dynamo has not derived the frontend from yet. Turning either
// into a diagnosis would put a failure word under the first pass of every rollout, which is how a
// check like this stops being read.
func TestAFrontendStillComingUpIsDegradedAndNotADiagnosis(t *testing.T) {
	starting := observedRollout(t, frontendRender(), append(readyPods(2), startingFrontendPods(2)...)...)
	if starting.phase() != phaseDegraded || starting.reason() != "Progressing" {
		t.Errorf("phase = %q, reason = %q while the frontend is still creating its container",
			starting.phase(), starting.reason())
	}
	none := observedRollout(t, frontendRender(), readyPods(2)...)
	if none.phase() != phaseDegraded || none.reason() != "Progressing" {
		t.Errorf("phase = %q, reason = %q before Dynamo has derived the frontend at all",
			none.phase(), none.reason())
	}
	// The same graph on a cluster that has not opted in is untouched by any of this.
	off := observedRollout(t, testRender, readyPods(2)...)
	if off.phase() != phaseRunning {
		t.Errorf("phase = %q on a cluster rendering no frontend of its own", off.phase())
	}
}

// Dynamo holds a graph's set of components immutable after creation, so a cluster that turns one
// frontend per model on cannot gain a frontend on the graphs it is already running. Observed on
// cluster-site-c: every reconcile of an existing deployment was refused, requeued, and
// refused again, and the only thing anywhere that said so was the same log line repeating.
//
// The remedy is a human's — deleting the graph reloads the weights — so what this pins is that the
// operator stops, says which object and which command, and reports it rather than retrying.
func TestAnImmutableComponentSetIsReportedAndNotRetriedForever(t *testing.T) {
	scheme := clientgoscheme.Scheme
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())

	refusal := apierrors.NewInvalid(
		schema.GroupKind{Group: "nvidia.com", Kind: "DynamoGraphDeployment"},
		render.WorkloadName(testDeploymentDesired()),
		field.ErrorList{field.Forbidden(field.NewPath("spec", "components"),
			"component topology is immutable and cannot be modified after creation: components added: [Frontend]")},
	)
	c := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true)).
		WithStatusSubresource(dep).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				if obj.GetObjectKind().GroupVersionKind().Kind == render.GVK.Kind {
					return refusal
				}
				return cl.Patch(ctx, obj, patch, opts...)
			},
		}).Build()

	r := &SyncedResourceReconciler{
		Client: c, Scheme: scheme, Log: slog.Default(),
		Namespace: "hangar-system", Render: frontendRender(),
		ClusterID: "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
	}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: dep.GetNamespace(), Name: dep.GetName()}}
	result, err := r.Reconcile(context.Background(), req)
	if err != nil {
		t.Fatalf("reconcile returned an error, so the controller would retry this for ever: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("RequeueAfter = %s, want no retry of a refusal only a human can clear", result.RequeueAfter)
	}

	var out v1alpha1.SyncedResource
	if err := c.Get(context.Background(), req.NamespacedName, &out); err != nil {
		t.Fatalf("read back: %v", err)
	}
	condition := readyCondition(t, &out)
	if condition.Reason != "TopologyImmutable" {
		t.Errorf("reason = %q, want TopologyImmutable", condition.Reason)
	}
	if !strings.Contains(condition.Message, "delete dynamographdeployment") {
		t.Errorf("message = %q, want the command that clears it", condition.Message)
	}
}

// graphOf reads back the graph deployment a reconcile rendered.
func graphOf(t *testing.T, r *SyncedResourceReconciler) *unstructured.Unstructured {
	t.Helper()
	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)
	key := types.NamespacedName{Namespace: "dynamo", Name: render.Name(testDeploymentID)}
	if err := r.Client.Get(context.Background(), key, graph); err != nil {
		t.Fatalf("read graph deployment: %v", err)
	}
	return graph
}

// reconcileAgain runs a second pass against the cluster the first one left behind.
func reconcileAgain(t *testing.T, r *SyncedResourceReconciler, res *v1alpha1.SyncedResource) {
	t.Helper()
	req := ctrl.Request{NamespacedName: types.NamespacedName{
		Namespace: res.GetNamespace(), Name: res.GetName()}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
}

// The 2026-09-02 fleet reload, as a test.
//
// Dynamo names each DynamoComponentDeployment after a hash of the component's whole pod template,
// so any difference in what this operator renders — including an ownership label nothing reads —
// abandons the running worker and starts a replacement that reloads the model's weights. An
// unconditional apply therefore makes every operator upgrade a fleet-wide reload, and on a
// saturated cluster a deadlock: the replacement cannot schedule until the original releases its
// accelerators, and the original is not being deleted.
//
// The live pod-template label below stands in for a renderer that has moved on. Nothing the plane
// declared has changed, so nothing is written and the running workload is left exactly as it is.
func TestARendererChangeDoesNotRollAWorkloadWhoseDeclarationStandsStill(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	graph := graphOf(t, r)
	components, _, err := unstructured.NestedSlice(graph.Object, "spec", "components")
	if err != nil || len(components) == 0 {
		t.Fatalf("spec.components: %v", err)
	}
	component := components[0].(map[string]any)
	if err := unstructured.SetNestedStringMap(component, map[string]string{
		"hangar.dev/deployment-id": testDeploymentID,
		"hangar.dev/managed-by":    "hangar-operator",
		"hangar.dev/project":       "prj_01J9X2",
		"hangar.dev/recipe":        "rec_01J9X2",
	}, "podTemplate", "metadata", "labels"); err != nil {
		t.Fatalf("set labels: %v", err)
	}
	if err := unstructured.SetNestedSlice(graph.Object, components, "spec", "components"); err != nil {
		t.Fatalf("set components: %v", err)
	}
	if err := r.Client.Update(context.Background(), graph); err != nil {
		t.Fatalf("update graph: %v", err)
	}
	before := graphOf(t, r).GetResourceVersion()

	reconcileAgain(t, r, out)

	after := graphOf(t, r)
	if after.GetResourceVersion() != before {
		t.Fatalf("graph was written on a pass that declared nothing new (resourceVersion %s -> %s); "+
			"every model in the fleet reloads its weights for this",
			before, after.GetResourceVersion())
	}
}

// The other half of the same rule. A guard that never lets a render through is not a guard, it is
// a broken operator — a declared change has to reach the cluster on the next pass.
func TestADeclaredChangeStillReachesTheCluster(t *testing.T) {
	desired := testDeploymentDesired()
	dep := declared(t, "deployment", testDeploymentID, desired)
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	want := fmt.Sprint(desired.Generation)
	if got := graphOf(t, r).GetAnnotations()[render.GenerationAnnotation]; got != want {
		t.Fatalf("%s = %q, want %s", render.GenerationAnnotation, got, want)
	}

	next := desired
	next.Generation++
	payload, err := json.Marshal(next)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	out.Spec.Payload = &apiextensionsv1.JSON{Raw: payload}
	out.Spec.Revision++
	if err := r.Client.Update(context.Background(), out); err != nil {
		t.Fatalf("update declared: %v", err)
	}

	reconcileAgain(t, r, out)

	want = fmt.Sprint(next.Generation)
	if got := graphOf(t, r).GetAnnotations()[render.GenerationAnnotation]; got != want {
		t.Fatalf("%s = %q, want %s — the declared change never reached the cluster",
			render.GenerationAnnotation, got, want)
	}
}
