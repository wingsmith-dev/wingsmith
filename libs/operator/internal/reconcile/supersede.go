package reconcile

import (
	"context"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
)

// componentItemGVK is one of the objects `componentGVK` lists.
//
// The listing kind already exists in diagnose.go, which only ever lists. Deleting needs the item
// kind as well, because a single object carrying a `...List` kind is not a thing the API server
// will accept a delete for.
var componentItemGVK = schema.GroupVersionKind{
	Group:   render.GVK.Group,
	Version: render.GVK.Version,
	Kind:    "DynamoComponentDeployment",
}

// componentNameLabel is how Dynamo marks which component of the graph one of these serves.
const componentNameLabel = "nvidia.com/dynamo-component"

// pruneSuperseded removes the component deployments Dynamo abandoned when the spec changed.
//
// The defect this exists for is NVIDIA's, not ours, and it cannot be fixed where it lives. Dynamo
// names each DynamoComponentDeployment `<graph>-<component>-<hash>`, where the hash is a digest of
// that component's spec — so every edit to a serving block produces a *new* name, and Dynamo's create
// path is "get by that name, not found, create". It never enumerates the ones it made before, so
// it never deletes them. Their ownerReference points at the graph deployment, which is still
// alive, so Kubernetes' garbage collector has no reason to touch them either. They simply
// accumulate, one per edit, each holding a full replica's worth of accelerators.
//
// On a cluster with spare cards that is waste. On a saturated one it is a deadlock that cannot
// resolve itself: the abandoned pod keeps its `nvidia.com/gpu` reservation while it crash-loops,
// so the new pod sits in `FailedScheduling: Insufficient nvidia.com/gpu` forever and the model
// never comes back. Observed on cluster-site-c, where three generations of one worker
// were alive at once on a node with eight cards.
//
// The signal is the graph deployment's own status. `status.components[<component>].componentNames`
// is Dynamo saying which objects are the current generation of that component; anything owned by
// this graph, for a component the graph still declares, that is not in that list has been
// superseded. That is an explicit statement rather than a heuristic on age or on a scheduler's
// message, which matters because a wrong answer here deletes a serving model.
//
// Three guards make a wrong answer unreachable:
//
//   - An empty current set prunes nothing. Between our apply and Dynamo's next pass the status is
//     the previous one, and a status that names nothing is a graph Dynamo has not reconciled yet.
//   - Only objects whose controlling owner is this exact graph deployment, by UID. A name is
//     reusable; a UID is not.
//   - Only objects strictly older than every current object *of their own component*. This is
//     what closes the race in the other direction: if Dynamo has created the new component but
//     not yet written the status naming it, the new one is younger than what the stale status
//     still names, and is left alone. Waiting one pass costs nothing and deleting the new one
//     costs a reload.
//
// A rolling update is the case where the status alone is not enough, and it is why `keep` is not
// simply `current`. While `status.rollingUpdate.phase` is `InProgress`, Dynamo names *both*
// generations of the rolling component in `componentNames` — observed on site-c on 2026-09-02, where
// the list held `...-23d3a70e` and `...-9e753aa8` together. Nothing is superseded against that
// set, so on a full node the roll never finishes: the new pod sits in `FailedScheduling:
// Insufficient nvidia.com/gpu` and the old one holds all eight cards for as long as anybody is
// willing to watch it. During a roll the newest current object of a component is the one kept and
// the rest of that component's current objects are treated as superseded, which is the same
// conclusion the paragraph below reaches for the ordinary case, reached one status earlier.
//
// Pruning happens on observing the supersession, not on observing the new pod fail to schedule.
// The tempting alternative — hold the old one until the new one is Pending on insufficient
// accelerators — reads the scheduler's refusal as the trigger, which makes correctness depend on
// a message string, only ever fires in the deadlock case, and leaves every non-saturated cluster
// running a second copy of the model into the same global Dynamo namespace, where the frontend
// can route to it. There is no blue/green to protect here: the plane declares one deployment at
// one generation, and this cluster's capacity check already admitted exactly one replica's
// worth of devices. Holding two generations at once is only possible where there is slack, and
// where there is not it is a guaranteed deadlock. At most one generation exists — the one the
// graph deployment names.
//
// This is reconciliation and not an operation (rule 1): it converges the cluster onto declared
// state on every pass, records nothing, and is safe to run when there is nothing to do. A
// read-only operator never reaches it, because a read-only operator never registers this
// controller at all.
func (r *SyncedResourceReconciler) pruneSuperseded(ctx context.Context, graphName string) error {
	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)
	err := r.Client.Get(ctx, types.NamespacedName{Namespace: r.Render.Namespace, Name: graphName}, graph)
	switch {
	case apierrors.IsNotFound(err), apimeta.IsNoMatchError(err):
		return nil
	case err != nil:
		return fmt.Errorf("read graph deployment %s: %w", graphName, err)
	}

	current := currentComponents(graph)
	if len(current) == 0 {
		return nil
	}

	list := &unstructured.UnstructuredList{}
	list.SetGroupVersionKind(componentGVK)
	err = r.Client.List(ctx, list,
		client.InNamespace(r.Render.Namespace),
		client.MatchingLabels{graphDeploymentLabel: graphName},
	)
	switch {
	case apimeta.IsNoMatchError(err):
		return nil
	case err != nil:
		return fmt.Errorf("list component deployments for %s: %w", graphName, err)
	}

	keep := keptByComponent(graph, list.Items, current)
	floors := oldestCurrentByComponent(list.Items, keep)
	if len(floors) == 0 {
		// The status names a generation none of whose objects are visible yet. Nothing here can
		// be called superseded against a set that is not there.
		return nil
	}

	uid := graph.GetUID()
	for i := range list.Items {
		obj := &list.Items[i]
		floor, known := floors[obj.GetLabels()[componentNameLabel]]
		switch {
		case keep[obj.GetName()]:
			continue
		case !controlledBy(obj, uid):
			continue
		// No current generation for this component means no floor to be older than. An object
		// whose component the status does not name yet is not something to reason about.
		case !known:
			continue
		case !obj.GetCreationTimestamp().Time.Before(floor.Time):
			continue
		case !r.declaresComponent(graph, obj):
			continue
		}
		switch err := r.Client.Delete(ctx, obj); {
		case err == nil:
			r.Log.Info("removed a superseded component deployment",
				"graph", graphName, "name", obj.GetName(), "namespace", obj.GetNamespace())
		case apierrors.IsNotFound(err):
		default:
			return fmt.Errorf("delete superseded component %s: %w", obj.GetName(), err)
		}
	}
	return nil
}

// keptByComponent narrows the current set to the objects that are not superseded.
//
// Outside a rolling update this is the current set unchanged, which is the whole of what the
// status was ever asked for. During one it is the newest current object of each component: a roll
// is Dynamo saying it has built the next generation and not yet retired the previous, and the
// previous is the one holding accelerators the next cannot get.
//
// A tie prunes nothing. Creation timestamps are second-granularity, so two objects of one
// component created in the same second cannot be ordered, and the safe reading of "cannot tell
// which is newer" is to keep both and look again next pass.
func keptByComponent(
	graph *unstructured.Unstructured,
	items []unstructured.Unstructured,
	current map[string]bool,
) map[string]bool {
	if !rollingUpdateInProgress(graph) {
		return current
	}

	newest := map[string]*unstructured.Unstructured{}
	tied := map[string]bool{}
	for i := range items {
		obj := &items[i]
		if !current[obj.GetName()] {
			continue
		}
		component := obj.GetLabels()[componentNameLabel]
		if component == "" {
			continue
		}
		held, seen := newest[component]
		switch {
		case !seen:
			newest[component] = obj
		case held.GetCreationTimestamp().Time.Before(obj.GetCreationTimestamp().Time):
			newest[component] = obj
			delete(tied, component)
		case obj.GetCreationTimestamp().Time.Equal(held.GetCreationTimestamp().Time):
			tied[component] = true
		}
	}

	keep := map[string]bool{}
	for name := range current {
		keep[name] = true
	}
	for i := range items {
		obj := &items[i]
		component := obj.GetLabels()[componentNameLabel]
		if component == "" || tied[component] || !current[obj.GetName()] {
			continue
		}
		if held, seen := newest[component]; seen && held.GetName() != obj.GetName() {
			delete(keep, obj.GetName())
		}
	}
	return keep
}

// rollingUpdateInProgress reports whether Dynamo says it is part-way through replacing a
// component of this graph.
func rollingUpdateInProgress(graph *unstructured.Unstructured) bool {
	phase, found, err := unstructured.NestedString(graph.Object, "status", "rollingUpdate", "phase")
	return err == nil && found && phase == "InProgress"
}

// currentComponents is the set of component deployment names the graph's status calls current.
func currentComponents(graph *unstructured.Unstructured) map[string]bool {
	components, found, err := unstructured.NestedMap(graph.Object, "status", "components")
	if err != nil || !found {
		return nil
	}
	out := map[string]bool{}
	for key := range components {
		names, found, err := unstructured.NestedStringSlice(graph.Object, "status", "components", key, "componentNames")
		if err != nil || !found {
			continue
		}
		for _, name := range names {
			if name != "" {
				out[name] = true
			}
		}
	}
	return out
}

// oldestCurrentByComponent is the earliest creation time among the current objects of each
// component, keyed by component name.
//
// Per component, and that is the whole of it. One floor across the graph was the conservative
// reading while a graph held one component, and it becomes a floor that prunes nothing the moment
// a graph holds two: a component nobody edits keeps the object it was created with, so its
// creation time — the graph's own birth — becomes a floor no superseded object can be older than.
// Every candidate is then skipped, silently and forever, which is the accumulation this whole
// function exists to stop. Timestamps here are second-granularity, so even the first generation
// ties rather than passing.
//
// An object carrying no component label contributes no floor and is looked up under the empty
// key, which never matches a labelled component. It is left alone, as `declaresComponent` also
// leaves it alone: it is not something this render produced.
func oldestCurrentByComponent(
	items []unstructured.Unstructured,
	kept map[string]bool,
) map[string]metav1.Time {
	floors := map[string]metav1.Time{}
	for i := range items {
		if !kept[items[i].GetName()] {
			continue
		}
		component := items[i].GetLabels()[componentNameLabel]
		if component == "" {
			continue
		}
		created := items[i].GetCreationTimestamp()
		if floor, seen := floors[component]; !seen || created.Time.Before(floor.Time) {
			floors[component] = created
		}
	}
	return floors
}

// controlledBy reports whether the graph deployment with this UID is the object's controller.
func controlledBy(obj *unstructured.Unstructured, uid types.UID) bool {
	if uid == "" {
		return false
	}
	for _, ref := range obj.GetOwnerReferences() {
		if ref.UID == uid && ref.Kind == render.GVK.Kind && ref.Controller != nil && *ref.Controller {
			return true
		}
	}
	return false
}

// declaresComponent reports whether the graph still asks for the component this object serves.
//
// A last check on the other axis. The status set answers "which generation", and this answers
// "which component" — an object labelled for a component the graph no longer declares is not
// something the status was ever going to name, and removing a component from the spec is a
// different change from editing one. An object carrying no component label is left alone: it is
// not something this render produced, and guessing would be the one mistake that is expensive.
func (r *SyncedResourceReconciler) declaresComponent(graph, obj *unstructured.Unstructured) bool {
	name := obj.GetLabels()[componentNameLabel]
	if name == "" {
		return false
	}
	components, found, err := unstructured.NestedSlice(graph.Object, "spec", "components")
	if err != nil || !found {
		return false
	}
	for _, raw := range components {
		component, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if declared, ok := component["name"].(string); ok && declared == name {
			return true
		}
	}
	return false
}
