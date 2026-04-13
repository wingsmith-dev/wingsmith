package reconcile

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// fieldOwner identifies this operator's writes to the API server.
//
// Every rendered object goes out as a server-side apply under this name, which is what lets the
// Dynamo operator own the fields it writes back without the two of us overwriting each other on
// every pass. It also means a field this operator stops rendering is removed rather than left
// behind, which hand-rolled create-then-update does not give.
const fieldOwner = client.FieldOwner("hangar-operator")

// waitForDependency is how long to wait before looking again for a declared object that has not
// arrived, or for weights that are still downloading.
const waitForDependency = 15 * time.Second

// resyncAfterApply is how long a successfully applied deployment waits before it is looked at
// again with nothing having happened to it.
//
// A watch is the fast path and this is the floor under it. The rendered object lives in a
// different namespace from the declared one, so it is reached by label rather than by owner
// reference, and a label-based watch is only as good as the labels surviving on the object. This
// is what makes a rendered object that was deleted, edited or never really created come back
// without anyone writing desired state again — the property the previous ownerReference-only
// design assumed it had and did not.
const resyncAfterApply = 5 * time.Minute

// renderedFinalizer holds a declared deployment open until what it rendered has been removed.
//
// Cross-namespace rendering is why this exists at all. Deleting the declared copy used to
// garbage-collect the workload through an owner reference; that reference was never valid across
// namespaces, so teardown is a code path now and this is the hook it hangs on.
const renderedFinalizer = "hangar.dev/rendered"

// reconcileDeployment renders one deployment into the objects that serve it.
//
// Three things have to be true before anything is applied: the recipe it names has arrived, the
// artifact that recipe serves has arrived, and those weights are already resident on this cluster.
// None of the three is an error when absent — they are orderings, and the answer to each is to
// wait.
func (r *SyncedResourceReconciler) reconcileDeployment(ctx context.Context, res *v1alpha1.SyncedResource) (ctrl.Result, error) {
	var dep wire.DeploymentDesired
	if err := decodePayload(res, &dep); err != nil {
		return r.fail(ctx, res, "PayloadUndecodable", err.Error())
	}

	recipe, err := r.recipe(ctx, dep.RecipeID)
	if err != nil {
		return ctrl.Result{}, err
	}
	if recipe == nil {
		return r.wait(ctx, res, "RecipeNotDeclared",
			fmt.Sprintf("recipe %s has not arrived yet", dep.RecipeID))
	}

	artifact, err := r.artifact(ctx, recipe.ArtifactID)
	if err != nil {
		return ctrl.Result{}, err
	}
	if artifact == nil {
		return r.wait(ctx, res, "ArtifactNotDeclared",
			fmt.Sprintf("artifact %s has not arrived yet", recipe.ArtifactID))
	}

	// The residency gate. Starting workers against a cold cache is not merely slow: several
	// replicas resolving the same HuggingFace cache race on lock files that do not work over
	// NFS, and the loser dies in a way that crash-loops the whole graph deployment. The plane
	// declares what should be warm and the cache controller fetches it; this waits.
	resident, err := r.weightsResident(ctx, recipe.ArtifactID)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !resident {
		return r.wait(ctx, res, "WeightsNotResident",
			fmt.Sprintf("waiting for %s to finish downloading", recipe.ArtifactID))
	}

	// Capacity is checked before rendering rather than left to the scheduler, because the
	// scheduler's answer to an impossible request is silence.
	fits, most, err := schedulable(ctx, r.Client, dep, render.NodeSelector(dep, r.Render))
	if err != nil {
		return ctrl.Result{}, err
	}
	if !fits {
		return r.wait(ctx, res, "NoSchedulableCapacity", fmt.Sprintf(
			"one replica needs %d accelerators of class %s and the largest usable node advertises %d; "+
				"if the nodes are present, the device plugin may not be installed",
			dep.Placement.DevicesPerReplica, dep.Placement.DeviceClass, most))
	}

	obj, err := render.DynamoGraphDeployment(render.Inputs{
		Deployment: dep,
		Recipe:     *recipe,
		Artifact:   *artifact,
	}, r.Render)
	if err != nil {
		return r.fail(ctx, res, "RenderFailed", err.Error())
	}
	render.Owned(obj, res, v1alpha1.GroupVersion.WithKind("SyncedResource"))

	// The finalizer goes on before the apply, never after. Between the two there is a window in
	// which a workload exists and nothing records that it has to be cleaned up, and ordering it
	// this way makes the window the harmless direction: a finalizer with nothing rendered under
	// it removes itself on the next teardown, where a workload with no finalizer is one nobody
	// will ever delete.
	if err := r.holdOpen(ctx, res); err != nil {
		return ctrl.Result{}, err
	}

	if err := r.Client.Patch(ctx, obj, client.Apply, fieldOwner, client.ForceOwnership); err != nil {
		return ctrl.Result{}, fmt.Errorf("apply %s: %w", obj.GetName(), err)
	}
	r.Log.Info("applied", "deployment", dep.DeploymentID, "generation", dep.Generation,
		"replicas", dep.MinReplicas, "namespace", obj.GetNamespace())

	result, err := r.ready(ctx, res, "Applied", fmt.Sprintf("serving %s at revision %d", recipe.Name, recipe.Version))
	if err != nil {
		return result, err
	}
	result.RequeueAfter = resyncAfterApply
	return result, nil
}

// holdOpen adds the teardown finalizer if it is not already there.
func (r *SyncedResourceReconciler) holdOpen(ctx context.Context, res *v1alpha1.SyncedResource) error {
	if controllerutil.ContainsFinalizer(res, renderedFinalizer) {
		return nil
	}
	patch := client.MergeFrom(res.DeepCopy())
	controllerutil.AddFinalizer(res, renderedFinalizer)
	if err := r.Client.Patch(ctx, res, patch); err != nil {
		return fmt.Errorf("add finalizer to %s: %w", res.Name, err)
	}
	return nil
}

// teardown removes what a declared deployment rendered, then lets it go.
//
// Deleting by name rather than by label, because the name is derived from the deployment id and is
// therefore the same answer the render would give — a label selector would also match a workload
// some future render put under the same deployment for a different purpose. Absent is success:
// the point is that nothing is left behind, not that this call is the one that removed it.
func (r *SyncedResourceReconciler) teardown(ctx context.Context, res *v1alpha1.SyncedResource) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(res, renderedFinalizer) {
		return ctrl.Result{}, nil
	}

	if id := deploymentID(res); id != "" {
		obj := &unstructured.Unstructured{}
		obj.SetGroupVersionKind(render.GVK)
		obj.SetNamespace(r.Render.Namespace)
		obj.SetName(render.Name(id))
		switch err := r.Client.Delete(ctx, obj); {
		case err == nil:
			r.Log.Info("removed rendered workload", "deployment", id, "namespace", r.Render.Namespace)
		case apierrors.IsNotFound(err) || apimeta.IsNoMatchError(err):
		default:
			return ctrl.Result{}, fmt.Errorf("delete rendered workload for %s: %w", id, err)
		}
	}

	patch := client.MergeFrom(res.DeepCopy())
	controllerutil.RemoveFinalizer(res, renderedFinalizer)
	if err := r.Client.Patch(ctx, res, patch); err != nil {
		return ctrl.Result{}, fmt.Errorf("release finalizer on %s: %w", res.Name, err)
	}
	r.forgetReported(deploymentID(res))
	return ctrl.Result{}, nil
}

// weightsResident reports whether an artifact's prefetch has run to completion on this cluster.
//
// The Job's own completion is the signal rather than anything this operator records separately.
// A count kept here would be a second copy of a fact the API server already holds, and the two
// would disagree the first time the Job was deleted by hand.
func (r *SyncedResourceReconciler) weightsResident(ctx context.Context, artifactID string) (bool, error) {
	var job batchv1.Job
	key := types.NamespacedName{Namespace: r.Render.Namespace, Name: render.PrefetchJobName(artifactID)}
	switch err := r.Client.Get(ctx, key, &job); {
	case apierrors.IsNotFound(err):
		return false, nil
	case err != nil:
		return false, fmt.Errorf("read prefetch job for %s: %w", artifactID, err)
	}
	return job.Status.Succeeded > 0, nil
}

func (r *SyncedResourceReconciler) recipe(ctx context.Context, id string) (*wire.RecipeDesired, error) {
	var out wire.RecipeDesired
	found, err := r.declared(ctx, "recipe", id, &out)
	if err != nil || !found {
		return nil, err
	}
	return &out, nil
}

func (r *SyncedResourceReconciler) artifact(ctx context.Context, id string) (*wire.ArtifactDesired, error) {
	var out wire.ArtifactDesired
	found, err := r.declared(ctx, "artifact", id, &out)
	if err != nil || !found {
		return nil, err
	}
	return &out, nil
}

// declared reads one declared object out of the durable local copy.
//
// A resource of the wrong kind under the expected name reads as absent rather than as an error.
// Names are derived from prefixed ids that do not collide across kinds, so the only way to see one
// is a bug, and treating it as absent waits instead of rendering something from the wrong payload.
func (r *SyncedResourceReconciler) declared(ctx context.Context, kind, id string, into any) (bool, error) {
	var res v1alpha1.SyncedResource
	key := types.NamespacedName{Namespace: r.Namespace, Name: render.Name(id)}
	switch err := r.Client.Get(ctx, key, &res); {
	case apierrors.IsNotFound(err):
		return false, nil
	case err != nil:
		return false, fmt.Errorf("read %s %s: %w", kind, id, err)
	}
	if res.Spec.Kind != kind {
		r.Log.Warn("declared object has an unexpected kind", "name", key.Name,
			"want", kind, "got", res.Spec.Kind)
		return false, nil
	}
	if err := decodePayload(&res, into); err != nil {
		return false, err
	}
	return true, nil
}

func decodePayload(res *v1alpha1.SyncedResource, into any) error {
	if res.Spec.Payload == nil || len(res.Spec.Payload.Raw) == 0 {
		return fmt.Errorf("%s %s carries no payload", res.Spec.Kind, res.Name)
	}
	if err := json.Unmarshal(res.Spec.Payload.Raw, into); err != nil {
		return fmt.Errorf("decode %s %s: %w", res.Spec.Kind, res.Name, err)
	}
	return nil
}

// ready records a successful reconcile and marks the revision observed.
func (r *SyncedResourceReconciler) ready(ctx context.Context, res *v1alpha1.SyncedResource, reason, message string) (ctrl.Result, error) {
	patch := client.MergeFrom(res.DeepCopy())
	res.Status.ObservedRevision = res.Spec.Revision
	res.Status.Phase = "Ready"
	setCondition(res, metav1.ConditionTrue, reason, message)
	if err := r.Client.Status().Patch(ctx, res, patch); err != nil {
		return ctrl.Result{}, err
	}
	r.report(res, phasePlaced, reason, message)
	return ctrl.Result{}, nil
}

// wait records that a precondition has not been met and asks to be called again.
//
// ObservedRevision is deliberately not advanced: nothing was applied, so claiming the revision was
// acted on would let the next pass skip it once the precondition finally clears.
func (r *SyncedResourceReconciler) wait(ctx context.Context, res *v1alpha1.SyncedResource, reason, message string) (ctrl.Result, error) {
	patch := client.MergeFrom(res.DeepCopy())
	res.Status.Phase = "Waiting"
	setCondition(res, metav1.ConditionFalse, reason, message)
	if err := r.Client.Status().Patch(ctx, res, patch); err != nil {
		return ctrl.Result{}, err
	}
	r.Log.Info("waiting", "name", res.Name, "reason", reason, "message", message)
	r.report(res, phasePending, reason, message)
	return ctrl.Result{RequeueAfter: waitForDependency}, nil
}

// fail records a reconcile that cannot succeed by being retried.
//
// It returns no error for the same reason the adoption refusal does not: requeueing with backoff
// and logging a failure forever describes a transient fault, and a payload that does not decode or
// a recipe that names no image is not transient. The next write of desired state is what fixes it,
// and that arrives as a new revision rather than as a retry.
func (r *SyncedResourceReconciler) fail(ctx context.Context, res *v1alpha1.SyncedResource, reason, message string) (ctrl.Result, error) {
	patch := client.MergeFrom(res.DeepCopy())
	res.Status.Phase = "Failed"
	setCondition(res, metav1.ConditionFalse, reason, message)
	r.Log.Error("reconcile failed", "name", res.Name, "reason", reason, "message", message)
	if err := r.Client.Status().Patch(ctx, res, patch); err != nil {
		return ctrl.Result{}, err
	}
	r.report(res, phaseFailed, reason, message)
	return ctrl.Result{}, nil
}

func setCondition(res *v1alpha1.SyncedResource, status metav1.ConditionStatus, reason, message string) {
	apimeta.SetStatusCondition(&res.Status.Conditions, metav1.Condition{
		Type:               "Ready",
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: res.Generation,
	})
}
