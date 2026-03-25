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
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

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

	if err := r.Client.Patch(ctx, obj, client.Apply, fieldOwner, client.ForceOwnership); err != nil {
		return ctrl.Result{}, fmt.Errorf("apply %s: %w", obj.GetName(), err)
	}
	r.Log.Info("applied", "deployment", dep.DeploymentID, "generation", dep.Generation,
		"replicas", dep.MinReplicas)

	return r.ready(ctx, res, "Applied", fmt.Sprintf("serving %s at revision %d", recipe.Name, recipe.Version))
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
	return ctrl.Result{}, r.Client.Status().Patch(ctx, res, patch)
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
	return ctrl.Result{}, r.Client.Status().Patch(ctx, res, patch)
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
