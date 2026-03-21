package reconcile

import (
	"context"
	"log/slog"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
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
