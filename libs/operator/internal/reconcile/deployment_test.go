package reconcile

import (
	"context"
	"encoding/json"
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
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

const (
	testDeploymentID = "dep_01j9x2gggggggggggggggggggg"
	testRecipeID     = "rec_01j9x2ffffffffffffffffffff"
	testArtifactID   = "art_01j9x2eeeeeeeeeeeeeeeeeeee"
	testImage        = "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1"
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
		DeploymentID: testDeploymentID,
		ClusterID:    "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
		ProjectID:    "prj_01j9x2jjjjjjjjjjjjjjjjjjjj",
		RecipeID:     testRecipeID,
		MinReplicas:  2,
		MaxReplicas:  8,
		Generation:   14,
		Placement:    wire.PlacementConstraints{DeviceClass: "h100-80gb-sxm", DevicesPerReplica: 2},
	}
}

func testRecipeDesired() wire.RecipeDesired {
	image := testImage
	return wire.RecipeDesired{
		RecipeID:      testRecipeID,
		ArtifactID:    testArtifactID,
		Name:          "qwen3.8-27b",
		Version:       7,
		Engine:        "vllm",
		EngineImage:   &image,
		Parallelism:   wire.RecipeParallelism{Tensor: 2, Pipeline: 1, Expert: 1},
		ContextLength: 262144,
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

func completedPrefetch() *batchv1.Job {
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: render.PrefetchJobName(testArtifactID), Namespace: "dynamo"},
		Status:     batchv1.JobStatus{Succeeded: 1},
	}
}

func runReconcile(t *testing.T, objects ...client.Object) (*SyncedResourceReconciler, *v1alpha1.SyncedResource, ctrl.Result) {
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

func TestDeploymentWaitsForItsRecipe(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	_, out, result := runReconcile(t, dep)

	if got := readyCondition(t, out).Reason; got != "RecipeNotDeclared" {
		t.Errorf("reason = %q, want RecipeNotDeclared", got)
	}
	if result.RequeueAfter == 0 {
		t.Error("a deployment waiting on its recipe must ask to be called again")
	}
	if out.Status.ObservedRevision != 0 {
		t.Error("nothing was applied, so the revision must not be marked observed")
	}
}

func TestDeploymentWaitsForItsArtifact(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	_, out, _ := runReconcile(t, dep, rec)

	if got := readyCondition(t, out).Reason; got != "ArtifactNotDeclared" {
		t.Errorf("reason = %q, want ArtifactNotDeclared", got)
	}
}

// The gate that matters. Several replicas resolving a cold shared cache race on lock files that
// do not hold over NFS, and the loser crash-loops the whole graph deployment.
func TestDeploymentWaitsForResidentWeights(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, rec, art)

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
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

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
func TestAnAppliedDeploymentIsLookedAtAgainWithoutBeingWrittenTo(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, _, result := runReconcile(t, dep, rec, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	if result.RequeueAfter != resyncAfterApply {
		t.Errorf("RequeueAfter = %s, want %s", result.RequeueAfter, resyncAfterApply)
	}
}

// Teardown is a code path rather than the API server's job, because the owner reference that used
// to do it cannot cross the namespace boundary this render puts the two objects on.
func TestDeletingADeclaredDeploymentRemovesWhatItRendered(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

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

func TestRecipeIsHeldWithoutClusterWork(t *testing.T) {
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	_, out, _ := runReconcile(t, rec)

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
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(), gpuNode("gpu-0", 0, true))

	condition := readyCondition(t, out)
	if condition.Reason != "NoSchedulableCapacity" {
		t.Errorf("reason = %q, want NoSchedulableCapacity", condition.Reason)
	}
	if !strings.Contains(condition.Message, "device plugin") {
		t.Errorf("message should name the likeliest cause, got %q", condition.Message)
	}
}

// Capacity spread thinly over several nodes does not satisfy one replica: a tensor-parallel
// replica cannot be split across machines at all.
func TestCapacityIsCountedPerNodeNotClusterWide(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(),
		gpuNode("gpu-0", 1, true), gpuNode("gpu-1", 1, true))

	if got := readyCondition(t, out).Reason; got != "NoSchedulableCapacity" {
		t.Errorf("reason = %q, want NoSchedulableCapacity: two devices on two nodes cannot run a two-device replica", got)
	}
}

// A drain in progress is when this matters most: the accelerators are still allocatable and still
// reported, and counting them would promise capacity that is deliberately being removed.
func TestCordonedNodesDoNotCountAsCapacity(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	draining := gpuNode("gpu-0", 8, true)
	draining.Spec.Unschedulable = true
	_, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(), draining)

	if got := readyCondition(t, out).Reason; got != "NoSchedulableCapacity" {
		t.Errorf("reason = %q, want NoSchedulableCapacity", got)
	}
}

func TestNodesOutsideThePlacementSelectorAreNotCounted(t *testing.T) {
	desired := testDeploymentDesired()
	desired.Placement.NodeSelector = map[string]string{"region": "eu-west-1"}
	dep := declared(t, "deployment", testDeploymentID, desired)
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	if got := readyCondition(t, out).Reason; got != "NoSchedulableCapacity" {
		t.Errorf("reason = %q, want NoSchedulableCapacity: the only usable node is in another region", got)
	}
}

// TestObservedDeploymentIsNeverApplied is the gate ADR 0009 turns on. Everything a managed
// deployment needs in order to render is present here — recipe, artifact, resident weights, a node
// with room — so if anything is written it is the authority mark that failed and not a missing
// input.
func TestObservedDeploymentIsNeverApplied(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	dep.Spec.Authority = v1alpha1.AuthorityObserved
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

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
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, _, _ := runReconcile(t, dep, rec, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)
	key := types.NamespacedName{Namespace: "dynamo", Name: render.Name(testDeploymentID)}
	if err := r.Client.Get(context.Background(), key, graph); err == nil {
		t.Fatal("an unmarked deployment was rendered into the cluster")
	}
}
