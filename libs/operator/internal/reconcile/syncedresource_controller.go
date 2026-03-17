package reconcile

import (
	"context"
	"log/slog"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
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
