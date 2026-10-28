package reconcile

import (
	"context"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/render"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// pipelined is a deployment whose devices do not have to share a node: one device per tensor
// group, two stages passing activations between them.
func pipelined() wire.DeploymentDesired {
	dep := testDeploymentDesired()
	dep.Serving.Parallelism = wire.Parallelism{Tensor: 1, Pipeline: 2, Expert: 1}
	return dep
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
// the old message blamed the device plugin: sending a person to look at the one component that
// was demonstrably working, on a cluster advertising its accelerators perfectly well.
func TestTheDevicePluginIsBlamedOnlyWhenNothingIsAdvertised(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, art, completedPrefetch(),
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
	dep := declared(t, "deployment", testDeploymentID, pipelined())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art, completedPrefetch(),
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
	dep := declared(t, "deployment", testDeploymentID, pipelined())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, art, completedPrefetch(),
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
// leader-worker backend renders one even though nothing set multinode.enabled. That is what
// observing the capability is for: installing Grove is enough, on its own.
func TestMultinodeIsEnabledByObservingABackend(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, pipelined())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcileCapable(t, capableOfMultinode{}, dep, art,
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
	dep := declared(t, "deployment", testDeploymentID, pipelined())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art, completedPrefetch(),
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
	desired := pipelined()
	desired.Placement.Topology = "single_domain"
	dep := declared(t, "deployment", testDeploymentID, desired)
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, art, completedPrefetch(),
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
// prevent, and the deployment would look placed while being exactly what was ruled out.
func TestSingleDomainPinsTheReplicaToOneFabric(t *testing.T) {
	withMultinode(t)
	desired := pipelined()
	desired.Placement.Topology = "single_domain"
	dep := declared(t, "deployment", testDeploymentID, desired)
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art, completedPrefetch(),
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
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	r, out, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true))

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

// heldBy is a running pod on a node holding n accelerators, optionally one of this deployment's.
func heldBy(name, node string, n int64, deploymentID string) *corev1.Pod {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "elsewhere", Labels: map[string]string{}},
		Spec: corev1.PodSpec{
			NodeName: node,
			Containers: []corev1.Container{{
				Name: "main",
				Resources: corev1.ResourceRequirements{
					Limits: corev1.ResourceList{acceleratorResource: *resource.NewQuantity(n, resource.DecimalSI)},
				},
			}},
		},
		Status: corev1.PodStatus{Phase: corev1.PodRunning},
	}
	if deploymentID != "" {
		pod.Labels[deploymentIDLabel] = deploymentID
	}
	return pod
}

func TestAcceleratorsHeldByOtherPodsAreNotCapacity(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, art, completedPrefetch(),
		gpuNode("gpu-0", 8, true), heldBy("other", "gpu-0", 7, "dep_01j9x2otherotherotherother"))

	condition := readyCondition(t, out)
	if condition.Reason != "NoSchedulableCapacity" {
		t.Fatalf("seven of eight cards are held and a replica needs two; reason %s: %s",
			condition.Reason, condition.Message)
	}
	if !strings.Contains(condition.Message, "1 free") {
		t.Errorf("the message should say what is free, got %q", condition.Message)
	}
}

func TestAFinishedPodHoldsNothing(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	done := heldBy("done", "gpu-0", 8, "")
	done.Status.Phase = corev1.PodSucceeded
	_, out, _ := runReconcile(t, dep, art, completedPrefetch(), gpuNode("gpu-0", 8, true), done)

	if condition := readyCondition(t, out); condition.Status != metav1.ConditionTrue {
		t.Fatalf("a completed pod's cards are free; got %s: %s", condition.Reason, condition.Message)
	}
}

// A new generation replaces the deployment's own pods, so the cards they hold are its to reuse.
func TestTheDeploymentsOwnPodsAreNotCountedAgainstIt(t *testing.T) {
	dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	art := declared(t, "artifact", testArtifactID, testArtifactDesired())
	_, out, _ := runReconcile(t, dep, art, completedPrefetch(),
		gpuNode("gpu-0", 2, true), heldBy("mine", "gpu-0", 2, testDeploymentID))

	if condition := readyCondition(t, out); condition.Status != metav1.ConditionTrue {
		t.Fatalf("the deployment's own cards must count as available to it; got %s: %s",
			condition.Reason, condition.Message)
	}
}

func TestATaintTheWorkerDoesNotTolerateRemovesTheNode(t *testing.T) {
	for _, tc := range []struct {
		name  string
		taint corev1.Taint
		fits  bool
	}{
		{"the GPU taint the worker tolerates", corev1.Taint{Key: "nvidia.com/gpu", Value: "true", Effect: corev1.TaintEffectNoSchedule}, true},
		{"the GPU taint with another value", corev1.Taint{Key: "nvidia.com/gpu", Value: "present", Effect: corev1.TaintEffectNoSchedule}, true},
		{"the GPU taint with no value", corev1.Taint{Key: "nvidia.com/gpu", Effect: corev1.TaintEffectNoSchedule}, true},
		{"a taint nothing tolerates", corev1.Taint{Key: "dedicated", Value: "training", Effect: corev1.TaintEffectNoSchedule}, false},
		{"a preference only", corev1.Taint{Key: "dedicated", Value: "training", Effect: corev1.TaintEffectPreferNoSchedule}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dep := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
			art := declared(t, "artifact", testArtifactID, testArtifactDesired())
			node := gpuNode("gpu-0", 8, true)
			node.Spec.Taints = []corev1.Taint{tc.taint}
			_, out, _ := runReconcile(t, dep, art, completedPrefetch(), node)

			condition := readyCondition(t, out)
			if got := condition.Status == metav1.ConditionTrue; got != tc.fits {
				t.Fatalf("fits = %v, want %v (%s: %s)", got, tc.fits, condition.Reason, condition.Message)
			}
		})
	}
}

// The precondition counts the nodes the rendered worker tolerates, so a configured toleration key
// or value changes which tainted nodes hold room, the same way it changes the pod spec.
func TestTheCapacityCheckUsesTheConfiguredGPUToleration(t *testing.T) {
	tainted := func(key, value string) *corev1.Node {
		node := gpuNode("gpu-0", 8, true)
		node.Spec.Taints = []corev1.Taint{{Key: key, Value: value, Effect: corev1.TaintEffectNoSchedule}}
		return node
	}
	for _, tc := range []struct {
		name string
		opts render.Options
		node *corev1.Node
		fits bool
	}{
		{"a configured key on a node tainted with it", render.Options{GPUTolerationKey: "example.com/accelerator"}, tainted("example.com/accelerator", "yes"), true},
		{"a configured key on a node tainted with the default key", render.Options{GPUTolerationKey: "example.com/accelerator"}, tainted("nvidia.com/gpu", "true"), false},
		{"a configured value that matches", render.Options{GPUTolerationValue: "present"}, tainted("nvidia.com/gpu", "present"), true},
		{"a configured value that does not match", render.Options{GPUTolerationValue: "present"}, tainted("nvidia.com/gpu", "true"), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := fake.NewClientBuilder().WithObjects(tc.node).Build()
			dep := testDeploymentDesired()
			got, err := schedulable(context.Background(), c, dep, render.NodeSelector(dep, testRender), render.WorkerTolerations(tc.opts))
			if err != nil {
				t.Fatalf("schedulable: %v", err)
			}
			if got.OK != tc.fits {
				t.Fatalf("fits = %v, want %v (%s)", got.OK, tc.fits, got.Reason)
			}
		})
	}
}
