package reconcile

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/workload"
)

func servingDeployment(image string) *appsv1.Deployment {
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "llama-70b-vllm", Namespace: "serving"},
		Spec: appsv1.DeploymentSpec{
			Template: corev1.PodTemplateSpec{
				Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "engine", Image: image}}},
			},
		},
	}
}

func adoptedResource(f string) *v1alpha1.SyncedResource {
	return &v1alpha1.SyncedResource{
		ObjectMeta: metav1.ObjectMeta{Name: "dep-01j9x2", Namespace: "hangar-system"},
		Spec: v1alpha1.SyncedResourceSpec{
			Kind:     "deployment",
			Revision: 1,
			Adopt: &v1alpha1.AdoptRef{
				Namespace: "serving", Kind: "deployment", Name: "llama-70b-vllm",
				SpecFingerprint: f,
			},
		},
	}
}

func newReconciler(t *testing.T, res *v1alpha1.SyncedResource, live *appsv1.Deployment) (*SyncedResourceReconciler, *v1alpha1.SyncedResource) {
	t.Helper()
	scheme := clientgoscheme.Scheme
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	builder := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(res).WithStatusSubresource(res)
	if live != nil {
		builder = builder.WithObjects(live)
	}
	c := builder.Build()
	r := &SyncedResourceReconciler{Client: c, Scheme: scheme, Log: slog.Default()}

	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: res.Namespace, Name: res.Name}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var out v1alpha1.SyncedResource
	if err := c.Get(context.Background(), req.NamespacedName, &out); err != nil {
		t.Fatalf("get: %v", err)
	}
	return r, &out
}

// TestAnAdoptedWorkloadThatStillMatchesIsLeftAlone: the successful case does nothing, which is the
// entire point. The model keeps the weights it already has loaded.
func TestAnAdoptedWorkloadThatStillMatchesIsLeftAlone(t *testing.T) {
	live := servingDeployment("ghcr.io/acme/vllm:0.9.1")
	digest := workload.Fingerprint(&live.Spec.Template.Spec)

	_, out := newReconciler(t, adoptedResource(digest), live)

	if out.Status.Phase != "Adopted" {
		t.Fatalf("phase is %q, want Adopted", out.Status.Phase)
	}
	if out.Status.ObservedRevision != 1 {
		t.Fatalf("observed revision is %d, want 1", out.Status.ObservedRevision)
	}
}

// TestAnAdoptedWorkloadThatDriftedIsRefused: the reconciler must not close the gap. Converging
// would replace a serving pod template and reload every model behind it.
func TestAnAdoptedWorkloadThatDriftedIsRefused(t *testing.T) {
	live := servingDeployment("ghcr.io/acme/vllm:0.9.2")
	adoptedAt := workload.Fingerprint(&servingDeployment("ghcr.io/acme/vllm:0.9.1").Spec.Template.Spec)

	_, out := newReconciler(t, adoptedResource(adoptedAt), live)

	if out.Status.Phase != "AdoptionDrifted" {
		t.Fatalf("phase is %q, want AdoptionDrifted", out.Status.Phase)
	}
	if out.Status.ObservedRevision != 0 {
		t.Fatal("a refused reconcile marked the revision as observed, which would stop it being re-checked")
	}
	ready := conditionOf(out, "Ready")
	if ready == nil || ready.Status != metav1.ConditionFalse || ready.Reason != "FingerprintMismatch" {
		t.Fatalf("the refusal is not on status: %+v", out.Status.Conditions)
	}
}

// TestAnAdoptedWorkloadThatVanishedIsRefused: an object we cannot read is not an object we may
// recreate. Recreating it is a cold start of the whole workload.
func TestAnAdoptedWorkloadThatVanishedIsRefused(t *testing.T) {
	_, out := newReconciler(t, adoptedResource("sha256:deadbeef"), nil)

	if out.Status.Phase != "AdoptionUnverified" {
		t.Fatalf("phase is %q, want AdoptionUnverified", out.Status.Phase)
	}
}

// TestScalingDoesNotLookLikeDrift: replicas are not part of the digest, so scaling an adopted
// workload stays free. A gate that fired on every scale would be turned off.
func TestScalingDoesNotLookLikeDrift(t *testing.T) {
	one := servingDeployment("ghcr.io/acme/vllm:0.9.1")
	four := servingDeployment("ghcr.io/acme/vllm:0.9.1")
	replicas := int32(4)
	four.Spec.Replicas = &replicas

	if workload.Fingerprint(&one.Spec.Template.Spec) != workload.Fingerprint(&four.Spec.Template.Spec) {
		t.Fatal("changing the replica count changed the fingerprint")
	}
}

func conditionOf(res *v1alpha1.SyncedResource, name string) *metav1.Condition {
	for i := range res.Status.Conditions {
		if res.Status.Conditions[i].Type == name {
			return &res.Status.Conditions[i]
		}
	}
	return nil
}

// adoptedWithPayload is an adopted deployment the plane can be told about: the fingerprint gate
// plus the payload the report is built from. The bare `adoptedResource` carries no payload, which
// is enough to exercise the gate and not enough to exercise the reporting.
func adoptedWithPayload(t *testing.T, f string) *v1alpha1.SyncedResource {
	t.Helper()
	raw, err := json.Marshal(testDeploymentDesired())
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	res := adoptedResource(f)
	res.Spec.Payload = &apiextensionsv1.JSON{Raw: raw}
	return res
}

// selectedDeployment is a live adopted Deployment with a real selector and replica count, which is
// what the adopted rollout has to read its pods by — it has none of this operator's render labels.
func selectedDeployment(image string, replicas int32) *appsv1.Deployment {
	d := servingDeployment(image)
	d.Spec.Replicas = &replicas
	d.Spec.Selector = &metav1.LabelSelector{MatchLabels: map[string]string{"app": "llama-70b-vllm"}}
	return d
}

func servingPod(name string, ready bool) *corev1.Pod {
	status := corev1.ConditionFalse
	if ready {
		status = corev1.ConditionTrue
	}
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "serving",
			Labels:    map[string]string{"app": "llama-70b-vllm"},
		},
		Spec: corev1.PodSpec{NodeName: "node-a"},
		Status: corev1.PodStatus{
			Phase:      corev1.PodRunning,
			Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: status}},
		},
	}
}

// reconcileAdoptedWith runs one pass over an adopted resource with a reporter bound, and hands back
// what it tried to tell the plane along with the requeue it asked for.
func reconcileAdoptedWith(
	t *testing.T,
	res *v1alpha1.SyncedResource,
	live *appsv1.Deployment,
	pods ...*corev1.Pod,
) (*recordingReporter, ctrl.Result) {
	t.Helper()
	scheme := clientgoscheme.Scheme
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	builder := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(res).WithStatusSubresource(res)
	if live != nil {
		builder = builder.WithObjects(live)
	}
	for _, p := range pods {
		builder = builder.WithObjects(p)
	}
	c := builder.Build()
	r := &SyncedResourceReconciler{Client: c, Scheme: scheme, Log: slog.Default(), ClusterID: "clu_1"}
	rep := &recordingReporter{}
	r.Bind(rep)

	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: res.Namespace, Name: res.Name}}
	got, err := r.Reconcile(context.Background(), req)
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	return rep, got
}

// TestAnAdoptedWorkloadReportsWhatItIsRunning is the bug this path had in production: four adopted
// deployments were serving and every one of them read `0/1` in the console, because not applying
// anything was taken to mean there was nothing to say.
func TestAnAdoptedWorkloadReportsWhatItIsRunning(t *testing.T) {
	live := selectedDeployment("ghcr.io/acme/vllm:0.9.1", 2)
	digest := workload.Fingerprint(&live.Spec.Template.Spec)

	rep, _ := reconcileAdoptedWith(t, adoptedWithPayload(t, digest), live,
		servingPod("llama-70b-vllm-a", true),
		servingPod("llama-70b-vllm-b", true),
	)

	got := lastSent(t, rep)
	if got.ReadyReplicas != 2 || got.DesiredReplicas != 2 {
		t.Fatalf("reported %d/%d ready, want 2/2", got.ReadyReplicas, got.DesiredReplicas)
	}
	if got.Phase != phaseRunning || got.Reason != "Serving" {
		t.Fatalf("reported %s/%s, want running/Serving", got.Phase, got.Reason)
	}
	if len(rep.replicaFrames) != 2 {
		t.Fatalf("sent %d replica frames, want 2 — the rollup is counted from these",
			len(rep.replicaFrames))
	}
}

// TestAnAdoptedRolloutCountsOnlyReadyPods: a replica loading weights is not serving, and reporting
// it as ready tells the plane a model is reachable when nothing can reach it.
func TestAnAdoptedRolloutCountsOnlyReadyPods(t *testing.T) {
	live := selectedDeployment("ghcr.io/acme/vllm:0.9.1", 2)
	digest := workload.Fingerprint(&live.Spec.Template.Spec)

	rep, _ := reconcileAdoptedWith(t, adoptedWithPayload(t, digest), live,
		servingPod("llama-70b-vllm-a", true),
		servingPod("llama-70b-vllm-b", false),
	)

	got := lastSent(t, rep)
	if got.ReadyReplicas != 1 || got.UnavailableReplicas != 1 {
		t.Fatalf("reported %d ready / %d unavailable, want 1/1",
			got.ReadyReplicas, got.UnavailableReplicas)
	}
	if got.Phase != phaseDegraded {
		t.Fatalf("phase is %q, want degraded", got.Phase)
	}
}

// TestAnAdoptedWorkloadIsRequeued: neither watch can wake an adopted object — it carries none of
// the owner labels `declaredFor` maps back from — so without a requeue the fingerprint check runs
// once at startup and a workload that drifts afterwards is never noticed.
func TestAnAdoptedWorkloadIsRequeued(t *testing.T) {
	live := selectedDeployment("ghcr.io/acme/vllm:0.9.1", 1)
	digest := workload.Fingerprint(&live.Spec.Template.Spec)

	_, got := reconcileAdoptedWith(t, adoptedWithPayload(t, digest), live,
		servingPod("llama-70b-vllm-a", true))

	if got.RequeueAfter != resyncAfterApply {
		t.Fatalf("requeued after %v, want %v — a drift check that never runs again is not a check",
			got.RequeueAfter, resyncAfterApply)
	}
}

// TestADriftedAdoptedWorkloadIsRequeuedAndNotReported: the refusal is the fact that matters, and
// putting `Serving` on the plane's copy off counts read from a drifted object would bury it. It
// still has to come back, or the drift can never clear.
func TestADriftedAdoptedWorkloadIsRequeuedAndNotReported(t *testing.T) {
	live := selectedDeployment("ghcr.io/acme/vllm:0.9.1", 1)

	rep, got := reconcileAdoptedWith(t, adoptedWithPayload(t, "sha256:something-else"), live,
		servingPod("llama-70b-vllm-a", true))

	if len(rep.sent) != 0 {
		t.Fatalf("reported %+v about a drifted workload, want silence", rep.sent)
	}
	if got.RequeueAfter != resyncAfterApply {
		t.Fatalf("requeued after %v, want %v", got.RequeueAfter, resyncAfterApply)
	}
}

// cachedNamespaces refuses a read outside the namespaces the manager caches, the way the real
// cache does.
type cachedNamespaces struct {
	client.Client
	allowed map[string]bool
}

func (c cachedNamespaces) Get(ctx context.Context, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
	if key.Namespace != "" && !c.allowed[key.Namespace] {
		return fmt.Errorf("unable to get %s: unknown namespace for the cache", key)
	}
	return c.Client.Get(ctx, key, obj, opts...)
}

func (c cachedNamespaces) List(ctx context.Context, list client.ObjectList, opts ...client.ListOption) error {
	var lo client.ListOptions
	lo.ApplyOptions(opts)
	if !c.allowed[lo.Namespace] {
		return fmt.Errorf("unable to list in %q: unknown namespace for the cache", lo.Namespace)
	}
	return c.Client.List(ctx, list, opts...)
}

// TestAnAdoptedWorkloadOutsideTheCachedNamespacesIsRead: the manager caches the operator's and
// the workload namespace only, and an adopted workload can be anywhere. The adoption check reads
// the API server directly, so a workload in a third namespace is verified rather than refused.
func TestAnAdoptedWorkloadOutsideTheCachedNamespacesIsRead(t *testing.T) {
	live := servingDeployment("ghcr.io/acme/vllm:0.9.1")
	res := adoptedResource(workload.Fingerprint(&live.Spec.Template.Spec))

	scheme := clientgoscheme.Scheme
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	direct := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(res, live).WithStatusSubresource(res).Build()
	cached := cachedNamespaces{Client: direct, allowed: map[string]bool{"hangar-system": true, "dynamo": true}}

	for _, tc := range []struct {
		name   string
		reader client.Reader
		want   string
	}{
		{"through the cache alone", nil, "AdoptionUnverified"},
		{"through the API reader", direct, "Adopted"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := &SyncedResourceReconciler{Client: cached, Reader: tc.reader, Scheme: scheme, Log: slog.Default()}
			req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: res.Namespace, Name: res.Name}}
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatalf("reconcile: %v", err)
			}
			var out v1alpha1.SyncedResource
			if err := direct.Get(context.Background(), req.NamespacedName, &out); err != nil {
				t.Fatalf("get: %v", err)
			}
			if out.Status.Phase != tc.want {
				t.Fatalf("phase is %q, want %q", out.Status.Phase, tc.want)
			}
		})
	}
}

// TestAManagedAdoptedDeploymentIsNeverRenderedAsASecondCopy: managed authority on an adopted
// declaration used to send it down the render path, which put a second workload beside the one
// already running. The adoption path wins, and the condition says how to hand it over.
func TestAManagedAdoptedDeploymentIsNeverRenderedAsASecondCopy(t *testing.T) {
	live := servingDeployment("ghcr.io/acme/vllm:0.9.1")
	res := adoptedResource(workload.Fingerprint(&live.Spec.Template.Spec))
	res.Spec.Authority = v1alpha1.AuthorityManaged
	raw, err := json.Marshal(testDeploymentDesired())
	if err != nil {
		t.Fatal(err)
	}
	res.Spec.Payload = &apiextensionsv1.JSON{Raw: raw}

	r, out := newReconciler(t, res, live)

	if out.Status.Phase != "Adopted" {
		t.Fatalf("phase is %q, want Adopted", out.Status.Phase)
	}
	if ready := conditionOf(out, "Ready"); ready == nil || !strings.Contains(ready.Message, "without adopt") {
		t.Fatalf("the condition must say how to hand the workload over: %+v", out.Status.Conditions)
	}
	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)
	key := types.NamespacedName{Namespace: "dynamo", Name: render.WorkloadName(testDeploymentDesired())}
	if err := r.Client.Get(context.Background(), key, graph); err == nil {
		t.Fatalf("a graph deployment was rendered beside the adopted workload: %s", key)
	}
}
