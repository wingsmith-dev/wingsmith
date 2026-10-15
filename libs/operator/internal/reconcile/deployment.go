package reconcile

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

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
// Two things have to be true before anything is applied: the artifact it serves has arrived, and
// those weights are already resident on this cluster. Neither is an error when absent — they are
// orderings, and the answer to each is to wait. How to serve the model is not one of them: the
// serving block rides on the deployment itself, so it is never absent and never late.
func (r *SyncedResourceReconciler) reconcileDeployment(ctx context.Context, res *v1alpha1.SyncedResource) (ctrl.Result, error) {
	var dep wire.DeploymentDesired
	if err := decodePayload(res, &dep); err != nil {
		return r.fail(ctx, res, "PayloadUndecodable", err.Error())
	}

	// The instruction arrived and decoded. Reported before anything is checked, because the gap
	// this closes is the one at the very front: a deployment whose artifact is slow to arrive
	// spent its first minutes with the plane unable to say whether any cluster had even received
	// it.
	r.stage(res, dep, "Accepted", fmt.Sprintf("reconciling revision %d", res.Spec.Revision))

	artifactID := servedArtifact(dep)
	artifact, err := r.artifact(ctx, artifactID)
	if err != nil {
		return ctrl.Result{}, err
	}
	if artifact == nil {
		return r.wait(ctx, res, "ArtifactNotDeclared",
			fmt.Sprintf("artifact %s has not arrived yet", artifactID))
	}

	// The residency gate. Starting workers against a cold cache is not merely slow: several
	// replicas resolving the same HuggingFace cache race on lock files that do not work over
	// NFS, and the loser dies in a way that crash-loops the whole graph deployment. The plane
	// declares what should be warm and the cache controller fetches it; this waits.
	resident, err := r.weightsResident(ctx, artifactID)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !resident {
		return r.wait(ctx, res, "WeightsNotResident",
			fmt.Sprintf("waiting for %s to finish downloading", artifactID))
	}

	// Capacity is checked before rendering rather than left to the scheduler, because the
	// scheduler's answer to an impossible request is silence.
	placed, err := schedulable(ctx, r.Client, dep, render.NodeSelector(dep, r.Render))
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
	if placed.NodesPerReplica > 1 && !r.multinodeEnabled() {
		return r.wait(ctx, res, "MultinodeNotEnabled", fmt.Sprintf(
			"one replica needs %d nodes of %d accelerators and no leader-worker backend was found "+
				"in this cluster's API groups; install Grove (dynamo-platform.grove.enabled) or "+
				"LeaderWorkerSet",
			placed.NodesPerReplica, placed.DevicesPerNode))
	}

	// Every ordering is satisfied and the capacity is there. What is left is turning the serving
	// block into objects, which is the last step before the cluster has something to run.
	r.stage(res, dep, "Rendering",
		fmt.Sprintf("building %s from %s", dep.DeploymentID, dep.Serving.Digest))

	obj, err := render.DynamoGraphDeployment(render.Inputs{
		Deployment: dep,
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

	// Nothing is written when what is running was already built from these inputs. The apply
	// below is otherwise unconditional, and an unconditional apply makes every change to the
	// renderer a fleet-wide reload: Dynamo hashes a component's whole pod template to name the
	// DynamoComponentDeployment it creates, so a single byte of difference — on 2026-09-02 it was
	// an ownership label nothing read, dropped when Recipe was deleted — abandons the running
	// worker and starts a new one. Every model in the fleet reloaded its weights, and on a
	// saturated cluster it deadlocked: the new pod cannot schedule until the old one releases the
	// accelerators it is still holding.
	//
	// This is what the serving digest was for. Its definition is *would this produce the same
	// running process*, computed by the plane over its own declaration, so a comparison against
	// it is insensitive to how this operator happens to render that declaration today.
	//
	// The cost, stated plainly: while the declaration and the layout stand still, this operator
	// no longer corrects drift on the graph deployment, and a fix to the renderer does not reach
	// a running workload. Both are deliberate — a render that reaches a running workload without
	// anybody declaring a change is exactly the behaviour above. To force one, bump the
	// deployment's generation, or delete the graph deployment as `TopologyImmutable` describes;
	// either is a decision somebody made rather than a side effect of a rollout restart.
	current, err := r.renderIsCurrent(ctx, obj)
	if err != nil {
		return ctrl.Result{}, err
	}
	if current {
		r.Log.Info("already built from these inputs; not applying",
			"deployment", dep.DeploymentID, "generation", dep.Generation,
			"digest", dep.Serving.Digest, "namespace", obj.GetNamespace())
		return r.afterApply(ctx, res, dep, obj)
	}

	if err := r.Client.Patch(ctx, obj, client.Apply, fieldOwner, client.ForceOwnership); err != nil {
		if immutableTopology(err) {
			return r.fail(ctx, res, "TopologyImmutable", fmt.Sprintf(
				"%s already exists with a different set of components, and Dynamo refuses to "+
					"change the set on a live graph. Delete the graph deployment to have this "+
					"operator render it again from the current serving configuration: "+
					"kubectl -n %s delete dynamographdeployment %s. The model reloads its "+
					"weights, so this is a deliberate redeploy and not something to do while it "+
					"is serving.", obj.GetName(), obj.GetNamespace(), obj.GetName()))
		}
		if clusterRejected(err) {
			return r.fail(ctx, res, "ClusterRejected", fmt.Sprintf(
				"the cluster refused %s and will refuse it again unchanged: %s",
				obj.GetName(), err.Error()))
		}
		return ctrl.Result{}, fmt.Errorf("apply %s: %w", obj.GetName(), err)
	}
	r.Log.Info("applied", "deployment", dep.DeploymentID, "generation", dep.Generation,
		"replicas", dep.MinReplicas, "namespace", obj.GetNamespace())

	return r.afterApply(ctx, res, dep, obj)
}

// afterApply is everything that follows a graph deployment being current, written or not.
//
// It is reached from both sides of the apply because none of it is about the write. Collecting a
// renamed object, collecting one Dynamo abandoned, and reporting what is actually running are all
// statements about the cluster as it is now — a pass that wrote nothing still owes the plane the
// rollout, and a superseded component holding accelerators is no less stuck for the fact that
// this pass was not the one that created it.
func (r *SyncedResourceReconciler) afterApply(
	ctx context.Context,
	res *v1alpha1.SyncedResource,
	dep wire.DeploymentDesired,
	obj *unstructured.Unstructured,
) (ctrl.Result, error) {
	// A rename renders under a new object name, and the object the old name created is still
	// serving. Nothing else would ever collect it: the declared copy is not being deleted, so
	// teardown never runs, and the apply above only ever touches the current name.
	if err := r.pruneRendered(ctx, dep.DeploymentID, obj.GetName()); err != nil {
		return ctrl.Result{}, err
	}

	// An edit to the serving block renders the same graph deployment under the same name, and
	// Dynamo answers it by creating a component deployment under a new spec hash and abandoning
	// the previous one — which keeps its accelerators. See supersede.go; on a full node the new
	// pod can never schedule until the abandoned one is gone.
	if err := r.pruneSuperseded(ctx, obj.GetName()); err != nil {
		return ctrl.Result{}, err
	}

	applied := fmt.Sprintf("serving %s at generation %d", dep.ServedModelName, dep.Generation)
	if err := r.markReady(ctx, res, "Applied", applied); err != nil {
		return ctrl.Result{}, err
	}

	// Applied is a statement about a write, and the plane had nothing else — so a deployment
	// that was serving read `placed · 0/0 ready` forever, which looks exactly like one that
	// never came up. What is actually running is read back here and reported instead.
	//
	// A failure to read it is not a failure to reconcile: the apply succeeded. The plain
	// conclusion is reported in its place, which is what the plane used to get every time.
	out, err := r.observeRollout(ctx, dep)
	if err != nil {
		r.Log.Warn("could not read back what is running", "deployment", dep.DeploymentID, "error", err)
		r.report(res, phasePlaced, "Applied", applied)
		return ctrl.Result{RequeueAfter: resyncAfterApply}, nil
	}

	r.reportRollout(res, dep, out)

	if out.ready < out.desired || (out.frontendUnready && out.trouble == nil) {
		// Still filling in. Pod events are the fast path; this short requeue is the floor under
		// missed events, a restarting watch, or a worker or frontend pod that has not been created
		// yet. A named failure can rest on the drift floor because the watch will wake its recovery.
		return ctrl.Result{RequeueAfter: waitForDependency}, nil
	}
	return ctrl.Result{RequeueAfter: resyncAfterApply}, nil
}

// renderIsCurrent reports whether the live graph deployment was already built from these inputs.
//
// The three annotations `render` stamps are read off the live object and compared to the ones on
// the object just rendered. All three have to match, and any of them missing is a no: an object
// from an operator that predates one of these annotations has to be applied once so that it
// carries them, and the pass that does it is the last one that ever writes it unprompted.
//
// Read with a plain Get rather than from a cache, because the answer decides whether to write. A
// stale read here is a reload of a model, which is the expensive direction to be wrong in.
//
// Not found is not current — there is nothing running to leave alone. Neither is a cluster whose
// CRD is absent, which the apply reports better than a comparison could.
func (r *SyncedResourceReconciler) renderIsCurrent(
	ctx context.Context,
	rendered *unstructured.Unstructured,
) (bool, error) {
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(render.GVK)
	err := r.Client.Get(ctx, client.ObjectKeyFromObject(rendered), live)
	switch {
	case apierrors.IsNotFound(err), apimeta.IsNoMatchError(err):
		return false, nil
	case err != nil:
		return false, fmt.Errorf("read graph deployment %s: %w", rendered.GetName(), err)
	}

	want, got := rendered.GetAnnotations(), live.GetAnnotations()
	for _, key := range []string{
		render.GenerationAnnotation,
		render.ServingDigestAnnotation,
		render.LayoutAnnotation,
	} {
		if want[key] == "" || got[key] != want[key] {
			return false, nil
		}
	}
	return true, nil
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
	// Under an optimistic lock: finalizers are a list, and a plain merge patch replaces the whole
	// list with this copy's, dropping any finalizer written since it was read.
	patch := client.MergeFromWithOptions(res.DeepCopy(), client.MergeFromWithOptimisticLock{})
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

	id := deploymentID(res)
	if id != "" {
		if err := r.pruneRendered(ctx, id, ""); err != nil {
			return ctrl.Result{}, err
		}
	}

	patch := client.MergeFromWithOptions(res.DeepCopy(), client.MergeFromWithOptimisticLock{})
	controllerutil.RemoveFinalizer(res, renderedFinalizer)
	if err := r.Client.Patch(ctx, res, patch); err != nil {
		return ctrl.Result{}, fmt.Errorf("release finalizer on %s: %w", res.Name, err)
	}
	// Before forgetting, because the release frames are built from what this deployment was
	// remembered to be holding and `forgetReported` is what drops that memory.
	r.reportTornDown(id)
	r.forgetReported(id)
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
// would disagree the first time the Job was deleted by hand. Found by label, for the reason on
// prefetchJobFor: a name is a rendering, and a deployment held behind a fetch that already
// finished is the worst reading of the wrong one.
func (r *SyncedResourceReconciler) weightsResident(ctx context.Context, artifactID string) (bool, error) {
	job, err := r.prefetchJobFor(ctx, artifactID)
	if err != nil {
		return false, err
	}
	return job != nil && job.Status.Succeeded > 0, nil
}

// servedArtifact is the one artifact a deployment renders against.
//
// A deployment carries a list because a workload may one day serve weights and an adapter over
// them, and nothing renders the second yet. The first is the weights, which is what the ordering
// gate and the residency gate are both about.
func servedArtifact(dep wire.DeploymentDesired) string {
	if len(dep.ArtifactIDs) == 0 {
		return ""
	}
	return dep.ArtifactIDs[0]
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
// A resource of the wrong kind under the expected name reads as absent rather than as an error, and
// so does an empty id. Names are derived from prefixed ids that do not collide across kinds, so the
// only way to see either is a bug, and treating it as absent waits instead of rendering something
// from the wrong payload or asking the API server for a nameless object.
func (r *SyncedResourceReconciler) declared(ctx context.Context, kind, id string, into any) (bool, error) {
	if id == "" {
		return false, nil
	}
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
// a serving block that names no image is not transient. The next write of desired state is what fixes it,
// and that arrives as a new revision rather than as a retry.
// immutableTopology reports whether an apply was refused because the graph already exists with a
// different set of components.
//
// Dynamo's validating webhook holds `spec.components` immutable after creation, so a cluster that
// turns one frontend per model on cannot gain a frontend on the graphs it is already running:
//
//	admission webhook "vdynamographdeployment.kb.io" denied the request:
//	spec.components: Forbidden: component topology is immutable and cannot be modified
//	after creation: components added: [Frontend]
//
// Without this the apply returns an error, the controller requeues it, and the same rejection
// repeats for as long as the deployment exists — a log full of one line, a deployment that never
// changes, and nothing anywhere saying that the remedy is to recreate the graph.
//
// It is deliberately not recreated here. The graph holds the running worker, so deleting it is a
// full reload of the weights — tens of minutes on the hardware this runs on — and an operator that
// did that on its own would take a serving model down as a side effect of a configuration flag
// somebody flipped. Saying so and stopping is the honest half.
//
// Matched on the webhook's own words, which is fragile and is the lesser fragility: the string is
// someone else's and may change, and if it does this returns false and the behaviour is exactly
// what it was before — a retry loop rather than a wrong answer.
func immutableTopology(err error) bool {
	if !apierrors.IsInvalid(err) && !apierrors.IsForbidden(err) {
		return false
	}
	return strings.Contains(err.Error(), "component topology is immutable")
}

// clusterRejected reports whether the cluster refused this object in a way a retry cannot fix.
//
// `Invalid`, `Forbidden` and `BadRequest` are the API server — or an admission webhook in front of
// it — saying the object is wrong, which it will still be on the next pass. Returning those as a
// controller-runtime error requeues them with backoff for ever and tells the plane nothing: the
// deployment sits at `Rendering`, the reason is a log line in a cluster somebody would have to go
// and read, and the console shows a workload that was dispatched and then stopped having a story.
// `ClusterRejected` is the reason MESSAGES.md already reserves for exactly this, and the API
// server's own sentence is the most useful message there is — it names the field.
//
// `diagnose` cannot cover it. That reads conditions off the objects Dynamo derived from ours, and
// a webhook denial means nothing was created to derive anything from, so the one rejection with no
// live object to inspect was the one with no reporter.
//
// Everything else — a conflict, a timeout, a 500 — stays a requeue. Those are the errors where
// trying again is the correct answer, and reporting a deployment as failed because the API server
// was briefly busy would be worse than saying nothing.
func clusterRejected(err error) bool {
	return apierrors.IsInvalid(err) || apierrors.IsForbidden(err) || apierrors.IsBadRequest(err)
}

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
