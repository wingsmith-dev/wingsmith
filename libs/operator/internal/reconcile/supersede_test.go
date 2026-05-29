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
	testGraphName = "kimi-k3"
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
// Dynamo operator names a component deployment after a hash of its spec, so a recipe edit makes a
// new one and abandons the old — which keeps its accelerators and deadlocks a full node.
func TestPruneSuperseded(t *testing.T) {
	current := "kimi-k3-vllmdecodeworker-0a26de60"
	stale := "kimi-k3-vllmdecodeworker-24273190"
	older := "kimi-k3-vllmdecodeworker-bc2809b8"

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
			graph: graphDeployment("kimi-k3-vllmdecodeworker-ffffffff"),
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

// TestPruneSupersededWithoutAGraphIsNotAnError — the graph is applied one call earlier and read
// back here, and a cluster where it has not landed yet is a wait rather than a failure.
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
// a graph held one component: adding a second — a frontend, created with the graph and never
// edited since — pins the earliest current creation at the graph's own birth, and every
// superseded worker is younger than that. The prune then finds nothing to do, silently and
// forever, and the abandoned generation keeps its accelerators.
func TestPruneSupersededIsNotBlockedByALongLivedSecondComponent(t *testing.T) {
	const frontendComponent = "Frontend"
	current := "kimi-k3-vllmdecodeworker-0a26de60"
	stale := "kimi-k3-vllmdecodeworker-24273190"
	frontend := "kimi-k3-frontend-9f13c7a2"

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
