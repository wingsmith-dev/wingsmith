package reconcile

import (
	"context"
	"log/slog"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
)

// FinalizerReleaser is the only controller that runs in read-only mode. It removes the teardown
// finalizer from a declaration being deleted, because no reconciler is running to tear down what it
// rendered and remove the finalizer itself.
//
// The store releases a declaration the hub deletes as it deletes it. This covers every other
// deletion: one made with kubectl, which is how an uninstall starts, or by a namespace going away.
// What the declaration rendered during an earlier read-write run is left in place for a person to
// remove.
type FinalizerReleaser struct {
	Client client.Client
	Reader client.Reader
	Log    *slog.Logger
}

// SetupReleaser registers the read-only finalizer releaser.
func SetupReleaser(mgr ctrl.Manager, logger *slog.Logger) error {
	r := &FinalizerReleaser{
		Client: mgr.GetClient(),
		Reader: mgr.GetAPIReader(),
		Log:    logger.With("controller", "finalizer-release"),
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&v1alpha1.SyncedResource{}, builder.WithPredicates(heldInDeletion())).
		Named("syncedresource-release").
		Complete(r)
}

// heldInDeletion passes only a declaration that is being deleted and still carries the finalizer.
func heldInDeletion() predicate.Predicate {
	held := func(obj client.Object) bool {
		return obj != nil && !obj.GetDeletionTimestamp().IsZero() &&
			controllerutil.ContainsFinalizer(obj, renderedFinalizer)
	}
	return predicate.Funcs{
		CreateFunc:  func(e event.CreateEvent) bool { return held(e.Object) },
		UpdateFunc:  func(e event.UpdateEvent) bool { return held(e.ObjectNew) },
		DeleteFunc:  func(event.DeleteEvent) bool { return false },
		GenericFunc: func(e event.GenericEvent) bool { return held(e.Object) },
	}
}

// Reconcile implements reconcile.Reconciler.
func (r *FinalizerReleaser) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	return ctrl.Result{}, releaseFinalizer(ctx, r.Client, r.Reader, req.NamespacedName, r.Log)
}

// releaseFinalizer removes the teardown finalizer from a declaration that is being deleted. It
// reads uncached and writes under an optimistic lock, so it never removes a finalizer from an
// object whose deletion was cancelled by a recreate it has not seen.
func releaseFinalizer(ctx context.Context, c client.Client, reader client.Reader, key types.NamespacedName, log *slog.Logger) error {
	if reader == nil {
		reader = c
	}
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var obj v1alpha1.SyncedResource
		switch err := reader.Get(ctx, key, &obj); {
		case apierrors.IsNotFound(err):
			return nil
		case err != nil:
			return err
		}
		if obj.DeletionTimestamp.IsZero() || !controllerutil.ContainsFinalizer(&obj, renderedFinalizer) {
			return nil
		}
		patch := client.MergeFromWithOptions(obj.DeepCopy(), client.MergeFromWithOptimisticLock{})
		controllerutil.RemoveFinalizer(&obj, renderedFinalizer)
		if err := c.Patch(ctx, &obj, patch); err != nil {
			return err
		}
		log.Warn("read-only: released a deleted declaration without tearing down what it rendered",
			"name", key.Name)
		return nil
	})
}
