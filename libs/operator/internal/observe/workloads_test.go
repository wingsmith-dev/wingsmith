package observe

import (
	"context"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func servingDeployment(name string, replicas int32, ready int32, labels map[string]string) *appsv1.Deployment {
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:        name,
			Namespace:   "serving",
			Labels:      labels,
			Annotations: map[string]string{annotationModel: "openai/gpt-oss-120b"},
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Template: corev1.PodTemplateSpec{
				Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "engine", Image: "ghcr.io/acme/vllm:0.9.1"}}},
			},
		},
		Status: appsv1.DeploymentStatus{ReadyReplicas: ready},
	}
}

func ownedGPUPod(name, node, rsName, hash string, gpus int64, labels map[string]string) *corev1.Pod {
	if labels == nil {
		labels = map[string]string{}
	}
	labels[labelPodTemplateHash] = hash
	controller := true
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "serving",
			Labels:    labels,
			OwnerReferences: []metav1.OwnerReference{
				{Kind: "ReplicaSet", Name: rsName, Controller: &controller},
			},
		},
		Spec: corev1.PodSpec{
			NodeName: node,
			Containers: []corev1.Container{{
				Name: "engine",
				Resources: corev1.ResourceRequirements{
					Limits: corev1.ResourceList{resourceGPU: *resource.NewQuantity(gpus, resource.DecimalSI)},
				},
			}},
		},
		Status: corev1.PodStatus{
			Phase:      corev1.PodRunning,
			Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}},
		},
	}
}

// TestDiscoveryFindsWorkloadsThePlaneNeverDeclared is the whole feature: an operator dropped into
// a cluster that is already serving reports what it found, tracing pods back to their Deployment
// without ever reading a ReplicaSet.
func TestDiscoveryFindsWorkloadsThePlaneNeverDeclared(t *testing.T) {
	d := newKubeDiscoverer(t,
		servingDeployment("gpt-oss-120b-vllm", 2, 2, nil),
		ownedGPUPod("gpt-oss-120b-vllm-7d9f4-aaaaa", "gpu-a", "gpt-oss-120b-vllm-7d9f4", "7d9f4", 4, nil),
		ownedGPUPod("gpt-oss-120b-vllm-7d9f4-bbbbb", "gpu-b", "gpt-oss-120b-vllm-7d9f4", "7d9f4", 4, nil),
	)

	snapshot, err := d.Discover(context.Background())
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	if len(snapshot.Workloads) != 1 {
		t.Fatalf("want 1 discovered workload, got %d: %+v", len(snapshot.Workloads), snapshot.Workloads)
	}
	w := snapshot.Workloads[0]
	if w.Name != "gpt-oss-120b-vllm" || w.Kind != kindDeployment || w.Namespace != "serving" {
		t.Fatalf("wrong identity: %+v", w)
	}
	if w.ReplicasDesired != 2 || w.ReplicasReady != 2 {
		t.Fatalf("want 2/2 replicas, got %d/%d", w.ReplicasDesired, w.ReplicasReady)
	}
	if w.DevicesTotal != 8 || w.DevicesPerReplica != 4 {
		t.Fatalf("want 8 devices over 4 per replica, got %d over %d", w.DevicesTotal, w.DevicesPerReplica)
	}
	if w.Model != "openai/gpt-oss-120b" {
		t.Fatalf("model hint not read: %q", w.Model)
	}
	if len(w.Images) != 1 || w.Images[0] != "ghcr.io/acme/vllm:0.9.1" {
		t.Fatalf("images not read: %v", w.Images)
	}
	if len(w.Nodes) != 2 {
		t.Fatalf("want both nodes, got %v", w.Nodes)
	}
}

// TestDiscoverySkipsWhatThePlaneDeclared: a deployment we put there is not a discovery. Offering
// it for adoption would invite an operator to adopt something they already own.
func TestDiscoverySkipsWhatThePlaneDeclared(t *testing.T) {
	managedLabels := map[string]string{labelManaged: "dep_01j9x2dddddddddddddddddddd"}
	d := newKubeDiscoverer(t,
		servingDeployment("mistral-7b", 1, 1, managedLabels),
		ownedGPUPod("mistral-7b-abc12-aaaaa", "gpu-a", "mistral-7b-abc12", "abc12", 1, map[string]string{
			labelManaged: "dep_01j9x2dddddddddddddddddddd",
		}),
	)

	snapshot, err := d.Discover(context.Background())
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	if len(snapshot.Workloads) != 0 {
		t.Fatalf("a declared deployment was reported as discovered: %+v", snapshot.Workloads)
	}
}

// TestDiscoverySkipsAnythingThisOperatorRendered: the deployment id only ever lands on what a
// deployment rendered, and Dynamo names the pods it derives after the graph, not after us.
// `managed-by` is on everything this operator writes, which is what makes the test complete.
func TestDiscoverySkipsAnythingThisOperatorRendered(t *testing.T) {
	rendered := map[string]string{labelManagedBy: managedByOperator}
	d := newKubeDiscoverer(t,
		servingDeployment("mistral-7b", 1, 1, rendered),
		ownedGPUPod("mistral-7b-abc12-aaaaa", "gpu-a", "mistral-7b-abc12", "abc12", 1, rendered),
	)

	snapshot, err := d.Discover(context.Background())
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	if len(snapshot.Workloads) != 0 {
		t.Fatalf("something this operator rendered was reported as discovered: %+v", snapshot.Workloads)
	}
}

// TestDiscoveryIgnoresWorkloadsHoldingNoDevices: this is a GPU control plane's inventory, not a
// listing of everything running in the cluster.
func TestDiscoveryIgnoresWorkloadsHoldingNoDevices(t *testing.T) {
	pod := ownedGPUPod("web-abc12-aaaaa", "gpu-a", "web-abc12", "abc12", 0, nil)
	pod.Spec.Containers[0].Resources.Limits = corev1.ResourceList{}
	d := newKubeDiscoverer(t, servingDeployment("web", 1, 1, nil), pod)

	snapshot, err := d.Discover(context.Background())
	if err != nil {
		t.Fatalf("discover: %v", err)
	}
	if len(snapshot.Workloads) != 0 {
		t.Fatalf("a workload holding no accelerators was reported: %+v", snapshot.Workloads)
	}
}

// TestDiscoveredWorkloadIDsAreStable: the decision to track a workload is stored against this id,
// so two operators, or one operator across a restart, must derive the same one.
func TestDiscoveredWorkloadIDsAreStable(t *testing.T) {
	a := DiscoveredWorkloadID("clu_01j9x2aaaaaaaaaaaaaaaaaaaa", "serving", kindDeployment, "gpt-oss-120b-vllm")
	b := DiscoveredWorkloadID("clu_01j9x2aaaaaaaaaaaaaaaaaaaa", "serving", kindDeployment, "gpt-oss-120b-vllm")
	if a != b {
		t.Fatalf("id is not a pure function of the observation: %s != %s", a, b)
	}
	if other := DiscoveredWorkloadID("clu_01j9x2aaaaaaaaaaaaaaaaaaab", "serving", kindDeployment, "gpt-oss-120b-vllm"); other == a {
		t.Fatal("the same workload name in two clusters minted one id")
	}
	if len(a) != 30 || a[:4] != workloadPrefix {
		t.Fatalf("id does not have the contract's shape: %q", a)
	}
}

// TestObserverSendsDiscoveredWorkloadsOnceUntilTheyChange: an idle cluster stays off the wire, and
// re-sending an unchanged sighting would spend the queue's replacement slots for nothing.
func TestObserverSendsDiscoveredWorkloadsOnceUntilTheyChange(t *testing.T) {
	workload := Workload{
		Namespace: "serving", Name: "gpt-oss-120b-vllm", Kind: kindDeployment,
		ReplicasDesired: 2, ReplicasReady: 2, DevicesPerReplica: 4, DevicesTotal: 8,
		Nodes: []string{"gpu-a"},
	}
	discoverer := NewFake(Snapshot{Workloads: []Workload{workload}})
	o := newTestObserver(t, discoverer)
	sender := o.recorder(t)

	if err := o.Tick(context.Background()); err != nil {
		t.Fatalf("first tick: %v", err)
	}
	if got := len(sender.ofType(TypeWorkloadDiscovered)); got != 1 {
		t.Fatalf("want one sighting on the first tick, got %d", got)
	}
	if err := o.Tick(context.Background()); err != nil {
		t.Fatalf("second tick: %v", err)
	}
	if got := len(sender.ofType(TypeWorkloadDiscovered)); got != 1 {
		t.Fatalf("an unchanged sighting was re-sent: %d frames", got)
	}

	workload.ReplicasReady = 1
	discoverer.Set(Snapshot{Workloads: []Workload{workload}})
	if err := o.Tick(context.Background()); err != nil {
		t.Fatalf("third tick: %v", err)
	}
	if got := len(sender.ofType(TypeWorkloadDiscovered)); got != 2 {
		t.Fatalf("a changed sighting was not sent: %d frames", got)
	}
}
