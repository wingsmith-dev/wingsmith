package reconcile

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/workload"
)

// SyncedResourceReconciler converges the durable local copy of declared state onto the cluster.
//
// One controller over one CRD rather than one per domain kind. The kinds are not independent —
// a deployment cannot render without its recipe, its artifact, and those weights already being
// resident — so splitting them into separate controllers would buy separation and immediately
// spend it on cross-controller lookups for the same objects.
type SyncedResourceReconciler struct {
	Client client.Client
	Scheme *runtime.Scheme
	Log    *slog.Logger

	// Namespace is where the durable copies of declared state live.
	Namespace string

	// Render carries the per-cluster conventions a render needs and the plane does not declare.
	Render render.Options
}

// +kubebuilder:rbac:groups=placeholder.hangar.dev,resources=syncedresources,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=placeholder.hangar.dev,resources=syncedresources/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=coordination.k8s.io,resources=leases,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch
// +kubebuilder:rbac:groups="",resources=nodes,verbs=get;list;watch;patch;update
// +kubebuilder:rbac:groups="",resources=pods/eviction,verbs=create
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch
// +kubebuilder:rbac:groups=nvidia.com,resources=dynamographdeployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=nvidia.com,resources=dynamographdeployments/status,verbs=get
// +kubebuilder:rbac:groups=batch,resources=jobs,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups=batch,resources=jobs/status,verbs=get

// Reconcile implements the reconcile loop.
func (r *SyncedResourceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var res v1alpha1.SyncedResource
	if err := r.Client.Get(ctx, req.NamespacedName, &res); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// The adoption gate runs before the staleness check, not after. A resource whose revision
	// has already been observed still has to be re-examined, because the thing that drifts is
	// the cluster object and not the desired state describing it.
	if res.Spec.Adopt != nil {
		return r.reconcileAdopted(ctx, &res)
	}

	// Every kind is re-examined on every pass rather than skipped once its revision has been
	// observed. Desired state is only one of the inputs: a deployment also depends on a recipe
	// that may have arrived since, on weights that may have finished downloading since, and on
	// a rendered object that something else may have edited since. Gating on the revision alone
	// would make all three invisible until the plane happened to write again.

	switch res.Spec.Kind {
	case "deployment":
		return r.reconcileDeployment(ctx, &res)
	case "cache":
		return r.reconcileCache(ctx, &res)
	case "recipe", "artifact":
		// Nothing to converge on its own. Both are read by the deployment path, and an edit to
		// either re-triggers that path through the watch below rather than doing work here.
		return r.observed(ctx, &res)
	case "component":
		// Cluster-level dependencies — GPU operator, device plugin, storage, ingress. Driving
		// these to a declared version is a Helm release rather than an object to apply, which
		// is long-running and failable in a way an apply is not. It is deliberately not done
		// here yet.
		return r.observed(ctx, &res)
	default:
		return r.fail(ctx, &res, "UnknownKind", fmt.Sprintf("no reconciler for kind %q", res.Spec.Kind))
	}
}

// observed records a declared object this controller has nothing to apply for.
func (r *SyncedResourceReconciler) observed(ctx context.Context, res *v1alpha1.SyncedResource) (ctrl.Result, error) {
	if res.Status.ObservedRevision == res.Spec.Revision && res.Status.Phase == "Accepted" {
		return ctrl.Result{}, nil
	}
	patch := client.MergeFrom(res.DeepCopy())
	res.Status.ObservedRevision = res.Spec.Revision
	res.Status.Phase = "Accepted"
	setCondition(res, metav1.ConditionTrue, "NoClusterWork", "held as declared state; nothing to apply")
	return ctrl.Result{}, r.Client.Status().Patch(ctx, res, patch)
}

// SetupWithManager wires the reconciler to the manager.
//
// Owning the rendered objects is what closes the loop in the other direction: a graph deployment
// whose status changes, or a prefetch job that finishes downloading, wakes the declared object it
// came from without anything having to poll for it.
func (r *SyncedResourceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)

	return ctrl.NewControllerManagedBy(mgr).
		For(&v1alpha1.SyncedResource{}).
		Owns(&batchv1.Job{}).
		Owns(graph).
		Named("syncedresource").
		Complete(r)
}

// reconcileAdopted handles desired state promoted from a workload that was already running.
//
// It is the one place in this operator that is allowed to say no. An adopted object is not applied
// and never will be by this path: the plane promised it matches what is live, and the only useful
// thing to do is check that promise and refuse when it is broken. Converging instead would replace
// a serving pod template with a rendered one and reload every model behind it — the exact cost the
// whole adoption path exists to avoid.
//
// Refusing is not an error. Returning one would requeue with backoff and log a failure every time,
// when what actually happened is that a human has to look at a workload that drifted. The refusal
// goes on status, where it is visible and where the sync loop ships it upward.
func (r *SyncedResourceReconciler) reconcileAdopted(ctx context.Context, res *v1alpha1.SyncedResource) (ctrl.Result, error) {
	adopt := res.Spec.Adopt
	live, err := r.liveFingerprint(ctx, adopt)

	patch := client.MergeFrom(res.DeepCopy())
	condition := metav1.Condition{
		Type:               "Ready",
		ObservedGeneration: res.Generation,
		LastTransitionTime: metav1.Now(),
	}
	switch {
	case err != nil:
		res.Status.Phase = "AdoptionUnverified"
		condition.Status = metav1.ConditionFalse
		condition.Reason = "LiveObjectUnreadable"
		condition.Message = fmt.Sprintf("cannot read %s/%s %s: %v", adopt.Namespace, adopt.Name, adopt.Kind, err)
	case live != adopt.SpecFingerprint:
		res.Status.Phase = "AdoptionDrifted"
		condition.Status = metav1.ConditionFalse
		condition.Reason = "FingerprintMismatch"
		condition.Message = fmt.Sprintf(
			"%s/%s digests to %s but was adopted at %s; refusing to reconcile — converging would restart it",
			adopt.Namespace, adopt.Name, short(live), short(adopt.SpecFingerprint))
		r.Log.Warn("adopted workload drifted; refusing to reconcile",
			"namespace", adopt.Namespace, "name", adopt.Name, "live", live, "adopted", adopt.SpecFingerprint)
	default:
		res.Status.ObservedRevision = res.Spec.Revision
		res.Status.Phase = "Adopted"
		condition.Status = metav1.ConditionTrue
		condition.Reason = "MatchesAdoptedFingerprint"
		condition.Message = "running as adopted; no cluster work performed"
	}
	apimeta.SetStatusCondition(&res.Status.Conditions, condition)
	return ctrl.Result{}, r.Client.Status().Patch(ctx, res, patch)
}

// liveFingerprint digests the pod template of the object named by an adoption, as it is right now.
func (r *SyncedResourceReconciler) liveFingerprint(ctx context.Context, adopt *v1alpha1.AdoptRef) (string, error) {
	key := types.NamespacedName{Namespace: adopt.Namespace, Name: adopt.Name}
	switch strings.ToLower(adopt.Kind) {
	case "deployment":
		var d appsv1.Deployment
		if err := r.Client.Get(ctx, key, &d); err != nil {
			return "", err
		}
		return workload.Fingerprint(&d.Spec.Template.Spec), nil
	case "statefulset":
		var s appsv1.StatefulSet
		if err := r.Client.Get(ctx, key, &s); err != nil {
			return "", err
		}
		return workload.Fingerprint(&s.Spec.Template.Spec), nil
	case "pod":
		var p corev1.Pod
		if err := r.Client.Get(ctx, key, &p); err != nil {
			return "", err
		}
		return workload.FingerprintPod(&p), nil
	default:
		return "", fmt.Errorf("cannot verify an adopted %s", adopt.Kind)
	}
}

// short renders a digest at the length a human compares by eye.
func short(fingerprint string) string {
	trimmed := strings.TrimPrefix(fingerprint, "sha256:")
	if trimmed == "" {
		return "nothing"
	}
	if len(trimmed) > 12 {
		return trimmed[:12]
	}
	return trimmed
}
