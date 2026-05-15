package reconcile

import (
	"context"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// pipelined is the recipe of a replica whose devices do not have to share a node: one device per
// tensor group, two stages passing activations between them.
func pipelined() wire.RecipeDesired {
	rec := testRecipeDesired()
	rec.Parallelism = wire.RecipeParallelism{Tensor: 1, Pipeline: 2, Expert: 1}
	return rec
}

// inFabric puts a node in a named clique, which is how the operator learns that a fabric reaches
// between machines at all.
func inFabric(node *corev1.Node, clique string) *corev1.Node {
	node.Labels[render.CliqueLabel] = clique
	return node
}

// withMultinode turns on leader-worker rendering for one test, and puts it back afterwards.
func withMultinode(t *testing.T) {
	t.Helper()
	testRender.MultinodeEnabled = true
	t.Cleanup(func() { testRender.MultinodeEnabled = false })
}

// The bug this whole path exists for. Two one-device nodes cannot run a tensor group of two, and
// the old message blamed the device plugin — sending a person to look at the one component that
// was demonstrably working, on a cluster advertising its accelerators perfectly well.
func TestTheDevicePluginIsBlamedOnlyWhenNothingIsAdvertised(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(),
		gpuNode("gpu-0", 1, true), gpuNode("gpu-1", 1, true))

	message := readyCondition(t, out).Message
	if strings.Contains(message, "device plugin") {
		t.Errorf("these nodes advertise accelerators; the plugin is not the cause: %q", message)
	}
	if !strings.Contains(message, "tensor group") {
		t.Errorf("the message should name what does not fit, got %q", message)
	}
}

// The other half of the same fix. The same two nodes and the same two devices, laid out as two
// pipeline stages instead of one tensor group, are a replica this cluster can serve.
func TestAPipelinedReplicaSpansNodes(t *testing.T) {
	withMultinode(t)
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, pipelined())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(),
		gpuNode("gpu-0", 1, true), gpuNode("gpu-1", 1, true))

	if got := readyCondition(t, out).Reason; got != "Applied" {
		t.Fatalf("reason = %q, want Applied: two stages of one device fit two one-device nodes", got)
	}
	obj := renderedObject(t, r)
	component := firstComponent(t, obj)
	multinode, ok := component["multinode"].(map[string]any)
	if !ok {
		t.Fatal("a replica spanning machines must render as one leader-worker group")
	}
	if multinode["nodeCount"] != int64(2) {
		t.Errorf("nodeCount = %v, want 2", multinode["nodeCount"])
	}
	if got := renderedDevices(t, component); got != "1" {
		t.Errorf("devices per pod = %s, want 1: the request is per node, not per replica", got)
	}
}

// A cluster that has not installed Grove or LeaderWorkerSet renders a multi-node replica as a
// single pod holding a fraction of the devices the engine expects. It starts, then hangs in
// collective initialisation saying nothing. Refusing names the backend to install instead.
func TestMultinodeIsRefusedWhereItCannotBeRendered(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, pipelined())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(),
		gpuNode("gpu-0", 1, true), gpuNode("gpu-1", 1, true))

	condition := readyCondition(t, out)
	if condition.Reason != "MultinodeNotEnabled" {
		t.Fatalf("reason = %q, want MultinodeNotEnabled", condition.Reason)
	}
	if !strings.Contains(condition.Message, "Grove") {
		t.Errorf("the message should name the backend to install, got %q", condition.Message)
	}
}

// Detection is what enables multi-node, not the configured flag: a cluster that reports a
// leader-worker backend renders one even though nothing set multinode.enabled. This is the whole
// point of observing the capability — installing Grove is enough, on its own.
func TestMultinodeIsEnabledByObservingABackend(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, pipelined())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcileCapable(t, capableOfMultinode{}, dep, rec, art,
		completedPrefetch(), gpuNode("gpu-0", 1, true), gpuNode("gpu-1", 1, true))

	condition := readyCondition(t, out)
	if condition.Reason == "MultinodeNotEnabled" {
		t.Fatalf("a cluster reporting a backend must not be refused: %q", condition.Message)
	}
}

// capableOfMultinode is an observation loop that has seen a leader-worker backend.
type capableOfMultinode struct{}

func (capableOfMultinode) Multinode() bool { return true }

// A replica that fits on one machine belongs on one machine. Every node it spans is another hop
// between pipeline stages, and spreading one that did not need spreading buys nothing.
func TestAReplicaThatFitsOneNodeIsNotSpread(t *testing.T) {
	withMultinode(t)
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	rec := declared(t, "recipe", testRecipeID, pipelined())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(),
		gpuNode("gpu-0", 8, true), gpuNode("gpu-1", 8, true))

	if got := readyCondition(t, out).Reason; got != "Applied" {
		t.Fatalf("reason = %q, want Applied", got)
	}
	component := firstComponent(t, renderedObject(t, r))
	if _, spread := component["multinode"]; spread {
		t.Error("a two-device replica on eight-device nodes must not be rendered across machines")
	}
	if got := renderedDevices(t, component); got != "2" {
		t.Errorf("devices per pod = %s, want 2", got)
	}
}

// The declaration the operator used to drop on the floor. `single_domain` reached the cluster on
// every deployment and nothing read it, so a workload that asked to stay inside one fabric was
// spread over whatever the scheduler liked.
func TestSingleDomainRefusesNodesWithNoFabricBetweenThem(t *testing.T) {
	withMultinode(t)
	desired := testDeploymentDesired()
	desired.Placement.Topology = "single_domain"
	dep := declared(t, "deployment", testDeploymentID, desired)
	rec := declared(t, "recipe", testRecipeID, pipelined())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(),
		gpuNode("gpu-0", 1, true), gpuNode("gpu-1", 1, true))

	condition := readyCondition(t, out)
	if condition.Reason != "NoTopologyDomain" {
		t.Fatalf("reason = %q, want NoTopologyDomain: nothing asserts a fabric between these nodes", condition.Reason)
	}
	if !strings.Contains(condition.Message, render.CliqueLabel) {
		t.Errorf("the message should name the label that would assert one, got %q", condition.Message)
	}
}

// And where a fabric is asserted, the replica is pinned to it. Without the selector the scheduler
// is free to put one pod in each of two cliques, which is the split the declaration exists to
// prevent — and the deployment would look placed while being exactly what was ruled out.
func TestSingleDomainPinsTheReplicaToOneFabric(t *testing.T) {
	withMultinode(t)
	desired := testDeploymentDesired()
	desired.Placement.Topology = "single_domain"
	dep := declared(t, "deployment", testDeploymentID, desired)
	rec := declared(t, "recipe", testRecipeID, pipelined())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(),
		inFabric(gpuNode("gpu-0", 1, true), "rack-a"),
		inFabric(gpuNode("gpu-1", 1, true), "rack-a"),
		inFabric(gpuNode("gpu-2", 1, true), "rack-b"))

	if got := readyCondition(t, out).Reason; got != "Applied" {
		t.Fatalf("reason = %q, want Applied", got)
	}
	spec := podSpecOf(t, firstComponent(t, renderedObject(t, r)))
	selector, _ := spec["nodeSelector"].(map[string]any)
	if selector[render.CliqueLabel] != "rack-a" {
		t.Errorf("nodeSelector[%s] = %v, want rack-a", render.CliqueLabel, selector[render.CliqueLabel])
	}
}

// A single-node replica needs no pin: the node it lands on is one domain by definition, and
// pinning it to a clique would refuse every cluster that labels no fabric at all.
func TestASingleNodeReplicaIsNotPinnedToAClique(t *testing.T) {
	desired := testDeploymentDesired()
	desired.Placement.Topology = "single_domain"
	dep := declared(t, "deployment", testDeploymentID, desired)
	rec := declared(t, "recipe", testRecipeID, testRecipeDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, rec, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

	if got := readyCondition(t, out).Reason; got != "Applied" {
		t.Fatalf("reason = %q, want Applied", got)
	}
	spec := podSpecOf(t, firstComponent(t, renderedObject(t, r)))
	selector, _ := spec["nodeSelector"].(map[string]any)
	if _, pinned := selector[render.CliqueLabel]; pinned {
		t.Error("a replica on one node must not be confined to a labelled fabric")
	}
}

func renderedObject(t *testing.T, r *SyncedResourceReconciler) *unstructured.Unstructured {
	t.Helper()
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(render.GVK)
	name := render.WorkloadName(testDeploymentDesired())
	key := types.NamespacedName{Namespace: testRender.Namespace, Name: name}
	if err := r.Client.Get(context.Background(), key, obj); err != nil {
		t.Fatalf("get %s: %v", name, err)
	}
	return obj
}

func firstComponent(t *testing.T, obj *unstructured.Unstructured) map[string]any {
	t.Helper()
	components, ok, err := unstructured.NestedSlice(obj.Object, "spec", "components")
	if err != nil || !ok || len(components) == 0 {
		t.Fatalf("no components on %s: %v", obj.GetName(), err)
	}
	component, ok := components[0].(map[string]any)
	if !ok {
		t.Fatal("component is not an object")
	}
	return component
}

func podSpecOf(t *testing.T, component map[string]any) map[string]any {
	t.Helper()
	spec, ok, err := unstructured.NestedMap(component, "podTemplate", "spec")
	if err != nil || !ok {
		t.Fatalf("no pod spec on the component: %v", err)
	}
	return spec
}

func renderedDevices(t *testing.T, component map[string]any) string {
	t.Helper()
	containers, ok, err := unstructured.NestedSlice(podSpecOf(t, component), "containers")
	if err != nil || !ok || len(containers) == 0 {
		t.Fatalf("no containers: %v", err)
	}
	main, _ := containers[0].(map[string]any)
	devices, _, err := unstructured.NestedString(main, "resources", "limits", "nvidia.com/gpu")
	if err != nil {
		t.Fatalf("read the device limit: %v", err)
	}
	return devices
}
