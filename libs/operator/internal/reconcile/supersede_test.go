package reconcile

import (
	"context"
	"log/slog"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
)

const (
	testGraphName = "llama-70b"
	testGraphUID  = types.UID("464b90c7-32e4-49b4-a3fe-83a991f240cc")
	testComponent = "VllmDecodeWorker"
)

// graphDeployment builds a rendered graph whose status names `currentNames` as the live
// generation, the way the Dynamo operator writes it back.
func graphDeployment(currentNames ...string) *unstructured.Unstructured {
	names := make([]any, 0, len(currentNames))
	for _, name := range currentNames {
		names = append(names, name)
	}
	obj := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": render.GVK.GroupVersion().String(),
		"kind":       render.GVK.Kind,
		"metadata": map[string]any{
			"name":      testGraphName,
			"namespace": testRender.Namespace,
			"uid":       string(testGraphUID),
		},
		"spec": map[string]any{
			"components": []any{map[string]any{"name": testComponent}},
		},
	}}
	if len(names) > 0 {
		status := map[string]any{
			"components": map[string]any{
				testComponent: map[string]any{"componentNames": names},
			},
		}
		obj.Object["status"] = status
	}
	return obj
}

// componentDeployment builds one of the per-component objects Dynamo derives from a graph.
type componentOpts struct {
	name      string
	age       time.Duration
	graph     string
	component string
	ownerUID  types.UID
	orphan    bool
}

func componentDeployment(o componentOpts) *unstructured.Unstructured {
	graph := o.graph
	if graph == "" {
		graph = testGraphName
	}
	component := o.component
	if component == "" {
		component = testComponent
	}
	uid := o.ownerUID
	if uid == "" {
		uid = testGraphUID
	}
	controller := true
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(componentItemGVK)
	obj.SetName(o.name)
	obj.SetNamespace(testRender.Namespace)
	obj.SetCreationTimestamp(metav1.NewTime(time.Now().Add(-o.age).Truncate(time.Second)))
	labels := map[string]string{graphDeploymentLabel: graph}
	if component != "-" {
		labels[componentNameLabel] = component
	}
	obj.SetLabels(labels)
	if !o.orphan {
		obj.SetOwnerReferences([]metav1.OwnerReference{{
			APIVersion: render.GVK.GroupVersion().String(),
			Kind:       render.GVK.Kind,
			Name:       graph,
			UID:        uid,
			Controller: &controller,
		}})
	}
	return obj
}

func pruneSupersededOn(t *testing.T, objects ...client.Object) client.Client {
	t.Helper()
	scheme := clientgoscheme.Scheme
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).Build()
	r := &SyncedResourceReconciler{
		Client: c, Scheme: scheme, Log: slog.Default(),
		Namespace: "hangar-system", Render: testRender,
	}
	if err := r.pruneSuperseded(context.Background(), testGraphName); err != nil {
		t.Fatalf("pruneSuperseded: %v", err)
	}
	return c
}

func componentExists(t *testing.T, c client.Client, name string) bool {
	t.Helper()
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(componentItemGVK)
	err := c.Get(context.Background(), types.NamespacedName{Namespace: testRender.Namespace, Name: name}, obj)
	return err == nil
}

// TestPruneSuperseded is the production defect and every guard against over-reaching on it. The
// Dynamo operator names a component deployment after a hash of its spec, so a serving edit makes a
// new one and abandons the old, which keeps its accelerators and deadlocks a full node.
func TestPruneSuperseded(t *testing.T) {
	current := "llama-70b-vllmdecodeworker-0a26de60"
	stale := "llama-70b-vllmdecodeworker-24273190"
	older := "llama-70b-vllmdecodeworker-bc2809b8"

	cases := []struct {
		name    string
		graph   *unstructured.Unstructured
		objects []*unstructured.Unstructured
		gone    []string
		kept    []string
	}{
		{
			name:  "the generation the status names survives and every earlier one goes",
			graph: graphDeployment(current),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: current, age: time.Minute}),
				componentDeployment(componentOpts{name: stale, age: time.Hour}),
				componentDeployment(componentOpts{name: older, age: 2 * time.Hour}),
			},
			gone: []string{stale, older},
			kept: []string{current},
		},
		{
			name:  "one generation on its own is left alone",
			graph: graphDeployment(current),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: current, age: time.Minute}),
			},
			kept: []string{current},
		},
		{
			name:  "a status naming nothing prunes nothing",
			graph: graphDeployment(),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: current, age: time.Minute}),
				componentDeployment(componentOpts{name: stale, age: time.Hour}),
			},
			kept: []string{current, stale},
		},
		{
			name:  "a component created before the status caught up is not the stale one",
			graph: graphDeployment(stale),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: current, age: time.Minute}),
				componentDeployment(componentOpts{name: stale, age: time.Hour}),
			},
			kept: []string{current, stale},
		},
		{
			name:  "a component owned by a different graph deployment is not ours to remove",
			graph: graphDeployment(current),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: current, age: time.Minute}),
				componentDeployment(componentOpts{name: stale, age: time.Hour, ownerUID: "some-other-uid"}),
			},
			kept: []string{current, stale},
		},
		{
			name:  "a component nothing controls is left where it is",
			graph: graphDeployment(current),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: current, age: time.Minute}),
				componentDeployment(componentOpts{name: stale, age: time.Hour, orphan: true}),
			},
			kept: []string{current, stale},
		},
		{
			name:  "a component for something the graph no longer declares is left alone",
			graph: graphDeployment(current),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: current, age: time.Minute}),
				componentDeployment(componentOpts{name: stale, age: time.Hour, component: "VllmPrefillWorker"}),
			},
			kept: []string{current, stale},
		},
		{
			name:  "a component carrying no component label is left alone",
			graph: graphDeployment(current),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: current, age: time.Minute}),
				componentDeployment(componentOpts{name: stale, age: time.Hour, component: "-"}),
			},
			kept: []string{current, stale},
		},
		{
			name:  "a status naming a generation that does not exist yet prunes nothing",
			graph: graphDeployment("llama-70b-vllmdecodeworker-ffffffff"),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: stale, age: time.Hour}),
			},
			kept: []string{stale},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			objects := []client.Object{tc.graph}
			for _, obj := range tc.objects {
				objects = append(objects, obj)
			}
			c := pruneSupersededOn(t, objects...)
			for _, name := range tc.gone {
				if componentExists(t, c, name) {
					t.Errorf("%s survived and is still holding its accelerators", name)
				}
			}
			for _, name := range tc.kept {
				if !componentExists(t, c, name) {
					t.Errorf("%s was removed and should not have been", name)
				}
			}
		})
	}
}

// TestPruneSupersededWithoutAGraphIsNotAnError: the graph is applied one call earlier and read
// back here, and a cluster where it has not landed yet is a wait instead of a failure.
func TestPruneSupersededWithoutAGraphIsNotAnError(t *testing.T) {
	scheme := clientgoscheme.Scheme
	c := fake.NewClientBuilder().WithScheme(scheme).Build()
	r := &SyncedResourceReconciler{Client: c, Scheme: scheme, Log: slog.Default(), Render: testRender}
	if err := r.pruneSuperseded(context.Background(), testGraphName); err != nil {
		t.Fatalf("pruneSuperseded on an absent graph: %v", err)
	}
}

// A component nobody edits must not become a floor that stops every other component being pruned.
//
// The floor is per component for this reason alone. One floor across the graph was harmless while
// a graph held one component: adding a second (a frontend, created with the graph and never
// edited since) pins the earliest current creation at the graph's own birth, and every
// superseded worker is younger than that. The prune then finds nothing to do, silently and
// forever, and the abandoned generation keeps its accelerators.
func TestPruneSupersededIsNotBlockedByALongLivedSecondComponent(t *testing.T) {
	const frontendComponent = "Frontend"
	current := "llama-70b-vllmdecodeworker-0a26de60"
	stale := "llama-70b-vllmdecodeworker-24273190"
	frontend := "llama-70b-frontend-9f13c7a2"

	graph := graphDeployment()
	graph.Object["spec"] = map[string]any{"components": []any{
		map[string]any{"name": testComponent},
		map[string]any{"name": frontendComponent},
	}}
	graph.Object["status"] = map[string]any{"components": map[string]any{
		testComponent:     map[string]any{"componentNames": []any{current}},
		frontendComponent: map[string]any{"componentNames": []any{frontend}},
	}}

	c := pruneSupersededOn(t, graph,
		// Created with the graph, and older than every generation of the worker.
		componentDeployment(componentOpts{name: frontend, component: frontendComponent, age: 10 * time.Hour}),
		componentDeployment(componentOpts{name: current, age: time.Hour}),
		componentDeployment(componentOpts{name: stale, age: 5 * time.Hour}),
	)

	if componentExists(t, c, stale) {
		t.Error("the superseded worker survived; the frontend's age became a floor nothing is older than")
	}
	if !componentExists(t, c, current) {
		t.Error("the current worker was deleted")
	}
	if !componentExists(t, c, frontend) {
		t.Error("the frontend was deleted; it is the current generation of its own component")
	}
}

// declaring adds components to the graph's spec, so that `declaresComponent` recognises objects
// labelled for them. `graphDeployment` declares the worker alone.
func declaring(obj *unstructured.Unstructured, names ...string) *unstructured.Unstructured {
	components, _, _ := unstructured.NestedSlice(obj.Object, "spec", "components")
	for _, name := range names {
		components = append(components, map[string]any{"name": name})
	}
	if err := unstructured.SetNestedSlice(obj.Object, components, "spec", "components"); err != nil {
		panic(err)
	}
	return obj
}

// rolling marks a graph the way Dynamo does while it is part-way through replacing a component.
func rolling(obj *unstructured.Unstructured) *unstructured.Unstructured {
	status, _ := obj.Object["status"].(map[string]any)
	if status == nil {
		status = map[string]any{}
		obj.Object["status"] = status
	}
	status["rollingUpdate"] = map[string]any{"phase": "InProgress"}
	return obj
}

// TestPruneSupersededDuringARollingUpdate: a roll on a full node must not deadlock.
//
// While a roll is in progress Dynamo names both generations of the rolling component as current,
// so neither is superseded by the status and nothing is pruned. On a node with no spare cards the
// replacement stays Pending on `Insufficient nvidia.com/gpu` for as long as the original holds
// them, and the original is current, so the roll never finishes on its own.
func TestPruneSupersededDuringARollingUpdate(t *testing.T) {
	next := "llama-70b-vllmdecodeworker-9e753aa8"
	previous := "llama-70b-vllmdecodeworker-23d3a70e"
	frontendNext := "llama-70b-frontend-11111111"
	frontendPrev := "llama-70b-frontend-22222222"

	cases := []struct {
		name    string
		graph   *unstructured.Unstructured
		objects []*unstructured.Unstructured
		gone    []string
		kept    []string
	}{
		{
			name:  "the previous generation goes even while the status still calls it current",
			graph: rolling(graphDeployment(previous, next)),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: next, age: time.Minute}),
				componentDeployment(componentOpts{name: previous, age: 4 * time.Hour}),
			},
			gone: []string{previous},
			kept: []string{next},
		},
		{
			name:  "the same two generations outside a roll are both left alone",
			graph: graphDeployment(previous, next),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: next, age: time.Minute}),
				componentDeployment(componentOpts{name: previous, age: 4 * time.Hour}),
			},
			kept: []string{previous, next},
		},
		{
			name:  "a roll of one component does not touch another that is not rolling",
			graph: rolling(declaring(graphDeployment(previous, next, frontendPrev), render.FrontendComponent)),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: next, age: time.Minute}),
				componentDeployment(componentOpts{name: previous, age: 4 * time.Hour}),
				componentDeployment(componentOpts{
					name: frontendPrev, component: render.FrontendComponent, age: 4 * time.Hour}),
			},
			gone: []string{previous},
			kept: []string{next, frontendPrev},
		},
		{
			name: "two components rolling at once each keep their own newest",
			graph: rolling(declaring(
				graphDeployment(previous, next, frontendPrev, frontendNext), render.FrontendComponent)),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: next, age: time.Minute}),
				componentDeployment(componentOpts{name: previous, age: 4 * time.Hour}),
				componentDeployment(componentOpts{
					name: frontendNext, component: render.FrontendComponent, age: time.Minute}),
				componentDeployment(componentOpts{
					name: frontendPrev, component: render.FrontendComponent, age: 4 * time.Hour}),
			},
			gone: []string{previous, frontendPrev},
			kept: []string{next, frontendNext},
		},
		{
			// Creation timestamps are second-granularity. Two objects of one component born in
			// the same second cannot be ordered, and deleting the wrong one costs a reload, so
			// the tie keeps both and the next pass looks again.
			name:  "a tie on creation time prunes nothing",
			graph: rolling(graphDeployment(previous, next)),
			objects: []*unstructured.Unstructured{
				componentDeployment(componentOpts{name: next, age: time.Minute}),
				componentDeployment(componentOpts{name: previous, age: time.Minute}),
			},
			kept: []string{previous, next},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			objects := []client.Object{tc.graph}
			for _, obj := range tc.objects {
				objects = append(objects, obj)
			}
			c := pruneSupersededOn(t, objects...)
			for _, name := range tc.gone {
				if componentExists(t, c, name) {
					t.Errorf("%s still exists; it holds the accelerators the roll needs", name)
				}
			}
			for _, name := range tc.kept {
				if !componentExists(t, c, name) {
					t.Errorf("%s was deleted; that is a model reloading its weights", name)
				}
			}
		})
	}
}
