package reconcile

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/workload"
)

// SyncedResourceReconciler is a no-op reconciler over the placeholder CRD. It demonstrates the
// loop and nothing else: it reads desired state out of the CR, does no Kubernetes work, and
// records what it observed back onto status, which is what the sync loop ships upward.
//
// The real reconciler that expands domain objects into Kubernetes objects belongs to whoever owns
// the object model.
type SyncedResourceReconciler struct {
	Client client.Client
	Scheme *runtime.Scheme
	Log    *slog.Logger
}

// +kubebuilder:rbac:groups=placeholder.hangar.dev,resources=syncedresources,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=placeholder.hangar.dev,resources=syncedresources/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=coordination.k8s.io,resources=leases,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch

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

	if res.Status.ObservedRevision == res.Spec.Revision {
		return ctrl.Result{}, nil
	}

	r.Log.Info("reconciling", "name", res.Name, "kind", res.Spec.Kind, "revision", res.Spec.Revision)

	patch := client.MergeFrom(res.DeepCopy())
	res.Status.ObservedRevision = res.Spec.Revision
	res.Status.Phase = "Reconciled"
	meta := metav1.Condition{
		Type:               "Ready",
		Status:             metav1.ConditionTrue,
		Reason:             "NoOpReconciler",
		Message:            "skeleton reconciler performed no cluster work",
		ObservedGeneration: res.Generation,
		LastTransitionTime: metav1.Now(),
	}
	res.Status.Conditions = upsertCondition(res.Status.Conditions, meta)
	if err := r.Client.Status().Patch(ctx, &res, patch); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

// SetupWithManager wires the reconciler to the manager.
func (r *SyncedResourceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&v1alpha1.SyncedResource{}).
		Named("syncedresource").
		Complete(r)
}

func upsertCondition(existing []metav1.Condition, c metav1.Condition) []metav1.Condition {
	for i := range existing {
		if existing[i].Type == c.Type {
			if existing[i].Status == c.Status {
				c.LastTransitionTime = existing[i].LastTransitionTime
			}
			existing[i] = c
			return existing
		}
	}
	return append(existing, c)
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
	res.Status.Conditions = upsertCondition(res.Status.Conditions, condition)
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
