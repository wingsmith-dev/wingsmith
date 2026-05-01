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

// deploymentIDLabel is the label the render stamps the plane's deployment id with. It is how a
// rendered object is found without knowing what it was named, which matters because the name
// follows the deployment's name and the name can change.
const deploymentIDLabel = "hangar.dev/deployment-id"

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

	// The instruction arrived and decoded. Reported before anything is checked, because the gap
	// this closes is the one at the very front: a deployment whose recipe is slow to arrive spent
	// its first minutes with the plane unable to say whether any cluster had even received it.
	r.stage(res, dep, "Accepted", fmt.Sprintf("reconciling revision %d", res.Spec.Revision))

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
	placed, err := schedulable(ctx, r.Client, dep, *recipe, render.NodeSelector(dep, r.Render))
	if err != nil {
		return ctrl.Result{}, err
	}
	if !placed.OK {
		return r.wait(ctx, res, placed.Code, placed.Reason)
	}

	// A replica the check could only place across machines, on a cluster that cannot run one.
	// Rendering it anyway produces a single pod holding a fraction of the devices the engine was
	// told to expect, which starts and then hangs in collective initialisation with nothing
	// saying why — so this waits, and names the thing that has to be installed.
	if placed.NodesPerReplica > 1 && !r.Render.MultinodeEnabled {
		return r.wait(ctx, res, "MultinodeNotEnabled", fmt.Sprintf(
			"one replica needs %d nodes of %d accelerators and this cluster does not run multi-node "+
				"replicas; install Grove or LeaderWorkerSet and set multinode.enabled",
			placed.NodesPerReplica, placed.DevicesPerNode))
	}

	// Every ordering is satisfied and the capacity is there. What is left is turning the recipe
	// into objects, which is the last step before the cluster has something to run.
	r.stage(res, dep, "Rendering", fmt.Sprintf("building %s from %s", dep.DeploymentID, recipe.Name))

	obj, err := render.DynamoGraphDeployment(render.Inputs{
		Deployment: dep,
		Recipe:     *recipe,
		Artifact:   *artifact,
		Layout: render.Layout{
			DevicesPerNode:  placed.DevicesPerNode,
			NodesPerReplica: placed.NodesPerReplica,
			Domain:          placed.Domain,
		},
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

	// A rename renders under a new object name, and the object the old name created is still
	// serving. Nothing else would ever collect it: the declared copy is not being deleted, so
	// teardown never runs, and the apply above only ever touches the current name.
	if err := r.pruneRendered(ctx, dep.DeploymentID, obj.GetName()); err != nil {
		return ctrl.Result{}, err
	}

	applied := fmt.Sprintf("serving %s at revision %d", recipe.Name, recipe.Version)
	if err := r.markReady(ctx, res, "Applied", applied); err != nil {
		return ctrl.Result{}, err
	}

	// Applied is a statement about a write, and the plane had nothing else — so a deployment
	// that was serving read `placed · 0/0 ready` forever, which looks exactly like one that
	// never came up. What is actually running is read back here and reported instead.
	//
	// A failure to read it is not a failure to reconcile: the apply succeeded. The plain
	// conclusion is reported in its place, which is what the plane used to get every time.
	out, err := r.observeRollout(ctx, dep, *recipe)
	if err != nil {
		r.Log.Warn("could not read back what is running", "deployment", dep.DeploymentID, "error", err)
		r.report(res, phasePlaced, "Applied", applied)
		return ctrl.Result{RequeueAfter: resyncAfterApply}, nil
	}

	r.reportRollout(res, dep, out)

	if out.ready < out.desired {
		// Still filling in. The drift floor is far too slow to watch a rollout with, and pods
		// are not watched — only the graph deployment is.
		return ctrl.Result{RequeueAfter: waitForDependency}, nil
	}
	return ctrl.Result{RequeueAfter: resyncAfterApply}, nil
}

// pruneUnlabelled collects an object this operator rendered before it stamped the label that finds
// one.
//
// The label is how a rendered object is normally found, precisely because the name follows the
// deployment's name and the name can change. That works for everything rendered since the label
// existed and for nothing rendered before it: an unlabelled object matches no selector, so the
// listing above cannot see it, and the apply only ever touches the current name. Nothing else in
// this operator would ever collect it, and in production one such object outlived a rename by days
// — holding an accelerator and serving a second copy of the same model into the same namespace as
// the live one.
//
// It is found by name instead, and only by the one name that is safe to assume: `render.Name(id)`
// is what `WorkloadName` falls back to for a deployment with no renderable name, so an object
// sitting there is one this operator rendered or would render. An object carrying somebody else's
// deployment id is left alone even so — a label that disagrees is evidence, and the only reading of
// it is that the object is not this deployment's.
func (r *SyncedResourceReconciler) pruneUnlabelled(ctx context.Context, id, keep string) error {
	name := render.Name(id)
	if name == "" || name == keep {
		return nil
	}

	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(render.GVK)
	err := r.Client.Get(ctx, types.NamespacedName{Namespace: r.Render.Namespace, Name: name}, obj)
	switch {
	case apierrors.IsNotFound(err), apimeta.IsNoMatchError(err):
		return nil
	case err != nil:
		return fmt.Errorf("get rendered workload %s: %w", name, err)
	}
	if owner := obj.GetLabels()[deploymentIDLabel]; owner != "" && owner != id {
		return nil
	}

	switch err := r.Client.Delete(ctx, obj); {
	case err == nil:
		r.Log.Info("removed a rendered workload that predates the deployment-id label",
			"deployment", id, "name", name, "namespace", r.Render.Namespace)
	case apierrors.IsNotFound(err):
	default:
		return fmt.Errorf("prune unlabelled %s: %w", name, err)
	}
	return nil
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
// By label rather than by name, because the rendered name is the deployment's name and a
// deployment can be renamed: deleting the name the payload carries now would leave whatever the
// previous name rendered running forever. The label is the id, which nothing renames. Absent is
// success: the point is that nothing is left behind, not that this call is the one that removed
// it.
func (r *SyncedResourceReconciler) teardown(ctx context.Context, res *v1alpha1.SyncedResource) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(res, renderedFinalizer) {
		return ctrl.Result{}, nil
	}

	if id := deploymentID(res); id != "" {
		if err := r.pruneRendered(ctx, id, ""); err != nil {
			return ctrl.Result{}, err
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

// pruneRendered deletes every graph deployment rendered for one deployment id except `keep`.
//
// An empty `keep` removes all of them, which is what teardown wants. A missing CRD or a namespace
// with nothing in it are both success — the postcondition is that nothing rendered for this
// deployment survives under another name, not that this call did the removing.
func (r *SyncedResourceReconciler) pruneRendered(ctx context.Context, id, keep string) error {
	list := &unstructured.UnstructuredList{}
	list.SetGroupVersionKind(render.GVK)
	err := r.Client.List(ctx, list,
		client.InNamespace(r.Render.Namespace),
		client.MatchingLabels{deploymentIDLabel: id},
	)
	switch {
	case apimeta.IsNoMatchError(err):
		return nil
	case err != nil:
		return fmt.Errorf("list rendered workloads for %s: %w", id, err)
	}
	if err := r.pruneUnlabelled(ctx, id, keep); err != nil {
		return err
	}
	for i := range list.Items {
		obj := &list.Items[i]
		if obj.GetName() == keep {
			continue
		}
		switch err := r.Client.Delete(ctx, obj); {
		case err == nil:
			r.Log.Info("removed rendered workload", "deployment", id,
				"name", obj.GetName(), "namespace", obj.GetNamespace())
		case apierrors.IsNotFound(err):
		default:
			return fmt.Errorf("delete rendered workload %s: %w", obj.GetName(), err)
		}
	}
	return nil
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

// ready records a successful reconcile, marks the revision observed, and tells the plane.
func (r *SyncedResourceReconciler) ready(ctx context.Context, res *v1alpha1.SyncedResource, reason, message string) (ctrl.Result, error) {
	if err := r.markReady(ctx, res, reason, message); err != nil {
		return ctrl.Result{}, err
	}
	r.report(res, phasePlaced, reason, message)
	return ctrl.Result{}, nil
}

// markReady is the local half of `ready`, for the one caller that reports something better.
//
// The deployment path reads back what is actually running and reports that instead. Doing both
// would not merely be redundant: the two frames carry different replica counts, so each would
// look like news against the other's remembered key and the pair would re-send on every single
// pass, forever. Splitting the write from the report is what keeps the suppression rule honest.
func (r *SyncedResourceReconciler) markReady(ctx context.Context, res *v1alpha1.SyncedResource, reason, message string) error {
	patch := client.MergeFrom(res.DeepCopy())
	res.Status.ObservedRevision = res.Spec.Revision
	res.Status.Phase = "Ready"
	setCondition(res, metav1.ConditionTrue, reason, message)
	return r.Client.Status().Patch(ctx, res, patch)
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
