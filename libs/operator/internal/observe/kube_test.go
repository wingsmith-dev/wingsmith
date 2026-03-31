package observe

import (
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func gpuNode(name string, labels, annotations map[string]string, ready, cordoned bool) *corev1.Node {
	status := corev1.ConditionTrue
	if !ready {
		status = corev1.ConditionFalse
	}
	return &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name, Labels: labels, Annotations: annotations},
		Spec:       corev1.NodeSpec{Unschedulable: cordoned},
		Status: corev1.NodeStatus{
			NodeInfo: corev1.NodeSystemInfo{
				KernelVersion:           "6.8.0-45-generic",
				ContainerRuntimeVersion: "containerd://1.7.27",
			},
			Conditions: []corev1.NodeCondition{
				{
					Type: corev1.NodeReady, Status: status, Reason: "KubeletReady",
					Message:            "kubelet is posting ready status",
					LastTransitionTime: metav1.NewTime(time.Now()),
				},
				{
					Type: corev1.NodeMemoryPressure, Status: corev1.ConditionFalse,
					Reason: "KubeletHasSufficientMemory", Message: "node has sufficient memory",
					LastTransitionTime: metav1.NewTime(time.Now()),
				},
			},
		},
	}
}

func gpuPod(name, node string, gpus int64) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default"},
		Spec: corev1.PodSpec{
			NodeName: node,
			Containers: []corev1.Container{{
				Name: "engine",
				Resources: corev1.ResourceRequirements{
					Limits: corev1.ResourceList{resourceGPU: *resource.NewQuantity(gpus, resource.DecimalSI)},
				},
			}},
		},
		Status: corev1.PodStatus{Phase: corev1.PodRunning},
	}
}

func newKubeDiscoverer(t *testing.T, objs ...runtime.Object) *KubeDiscoverer {
	t.Helper()
	c := fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithRuntimeObjects(objs...).Build()
	return &KubeDiscoverer{Client: c}
}

// TestKubeDiscoveryReadsLabelsNotHardware: the operator describes accelerators it never talks to.
// Everything comes from what the device plugin and NFD publish, which is why no GPU and no
// privileged access is needed to run this loop.
func TestKubeDiscoveryReadsLabelsNotHardware(t *testing.T) {
	node := gpuNode("gpu-h100-a-07",
		map[string]string{
			labelGPUCount:              "4",
			labelGPUProduct:            "NVIDIA-H100-80GB-HBM3",
			labelGPUMemory:             "81559",
			labelGPUClique:             "clique-0",
			labelMigStrategy:           "single",
			labelDriverFull:            "570.86.15",
			labelRegion:                "eu-west-1",
			labelZone:                  "eu-west-1b",
			labelPool:                  "h100-sxm-8x",
			"kubernetes.io/os":         "linux",
			"kubernetes.io/arch":       "amd64",
			"node-role.kubernetes.io/": "worker",
		},
		map[string]string{
			annotationDeviceUUIDs: "GPU-aaaa, GPU-bbbb, GPU-cccc, GPU-dddd",
			annotationUnhealthy:   "GPU-cccc",
		}, true, false)

	d := newKubeDiscoverer(t, node, gpuPod("engine-0", "gpu-h100-a-07", 2))
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Nodes) != 1 {
		t.Fatalf("discovered %d nodes", len(snap.Nodes))
	}
	n := snap.Nodes[0]
	if n.Pool != "h100-sxm-8x" || !n.Ready || n.Cordoned {
		t.Fatalf("node discovered as %+v", n)
	}
	if n.DriverVersion != "570.86.15" || n.RuntimeVersion != "containerd://1.7.27" {
		t.Fatalf("node versions are %q / %q", n.DriverVersion, n.RuntimeVersion)
	}
	if n.AllocatedDevices != 2 {
		t.Fatalf("counted %d allocated devices, want 2", n.AllocatedDevices)
	}
	if len(n.Devices) != 4 {
		t.Fatalf("discovered %d devices, want 4", len(n.Devices))
	}
	if got := n.Labels; got["region"] != "eu-west-1" || got["zone"] != "eu-west-1b" || got["fabric"] != "nvlink" {
		t.Fatalf("exported labels are %v", got)
	}
	if _, leaked := n.Labels["kubernetes.io/os"]; leaked {
		t.Fatal("the observed node is a mirror of Kubernetes, which it must not be")
	}

	byUUID := map[string]Device{}
	for _, dev := range n.Devices {
		byUUID[dev.UUID] = dev
	}
	healthy, bad := byUUID["GPU-aaaa"], byUUID["GPU-cccc"]
	if healthy.Health != HealthHealthy || bad.Health != HealthUnhealthy {
		t.Fatalf("health is %q / %q", healthy.Health, bad.Health)
	}
	if healthy.DeviceClass != "h100-80gb-sxm" {
		t.Fatalf("device class derived as %q", healthy.DeviceClass)
	}
	if healthy.MemoryTotalMB != 81559 || !healthy.Mig.Enabled {
		t.Fatalf("device discovered as %+v", healthy)
	}
	if healthy.DomainKind != "nvlink" || healthy.DomainKey != "clique-0" {
		t.Fatalf("device domain is %q/%q", healthy.DomainKind, healthy.DomainKey)
	}
}

// TestUnreadyNodeReportsUnknownHealth: a node we cannot reach is not a node with broken hardware.
func TestUnreadyNodeReportsUnknownHealth(t *testing.T) {
	node := gpuNode("gpu-h100-a-09", map[string]string{labelGPUCount: "2"}, nil, false, true)
	d := newKubeDiscoverer(t, node)
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	n := snap.Nodes[0]
	if n.Ready || !n.Cordoned {
		t.Fatalf("node discovered as ready=%v cordoned=%v", n.Ready, n.Cordoned)
	}
	for _, dev := range n.Devices {
		if dev.Health != HealthUnknown {
			t.Fatalf("device on an unreachable node reports %q", dev.Health)
		}
	}
	// Without published UUIDs devices fall back to node-and-index, which is stable for as long
	// as the hardware stays in the machine.
	if n.Devices[0].UUID != "gpu-h100-a-09-gpu0" {
		t.Fatalf("fallback uuid is %q", n.Devices[0].UUID)
	}
	if n.Devices[0].DomainKind != "pcie" {
		t.Fatalf("a node with no NVLink clique reported domain %q", n.Devices[0].DomainKind)
	}
}

// TestCPUOnlyNodesCarryNoDevices: the cluster's control-plane machines are members, not supply.
func TestCPUOnlyNodesCarryNoDevices(t *testing.T) {
	d := newKubeDiscoverer(t, gpuNode("cp-0", map[string]string{}, nil, true, false))
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Nodes[0].Devices) != 0 {
		t.Fatalf("a CPU node reported %d devices", len(snap.Nodes[0].Devices))
	}

	o := newTestObserver(t, d)
	if err := o.Tick(t.Context()); err != nil {
		t.Fatal(err)
	}
	r := o.recorder(t)
	if len(r.ofType(TypeDeviceObserved)) != 0 || len(r.ofType(TypeTopologyObserved)) != 0 {
		t.Fatal("a CPU-only cluster produced device or topology frames")
	}
	if len(r.ofType(TypeNodeObserved)) != 1 {
		t.Fatal("a CPU node is still a member and must be observed")
	}
}

func TestSnakeCasesKubernetesConditions(t *testing.T) {
	for in, want := range map[string]string{
		"MemoryPressure":             "memory_pressure",
		"PIDPressure":                "pid_pressure",
		"Ready":                      "ready",
		"KubeletHasSufficientMemory": "kubelet_has_sufficient_memory",
		"NetworkUnavailable":         "network_unavailable",
	} {
		if got := snake(in); got != want {
			t.Errorf("snake(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestDeviceClassPrecedence pins the order the class is decided in: the hangar label overrides
// everything, the dictionary names what it knows, and anything else is slugified rather than
// dropped, so a cluster with no hangar labels at all still groups its hardware.
func TestDeviceClassPrecedence(t *testing.T) {
	labelled := gpuNode("gpu-a", map[string]string{
		labelGPUCount:    "1",
		labelGPUProduct:  "NVIDIA-H100-80GB-HBM3",
		labelDeviceClass: "bespoke-class",
	}, nil, true, false)
	if got := projectKubeNode(labelled, nil, discardLog).Devices[0].DeviceClass; got != "bespoke-class" {
		t.Fatalf("label override lost: %q", got)
	}

	unknown := gpuNode("gpu-b", map[string]string{
		labelGPUCount:   "1",
		labelGPUProduct: "NVIDIA-Zephyr-4000-Ultra",
	}, nil, true, false)
	if got := projectKubeNode(unknown, nil, discardLog).Devices[0].DeviceClass; got != "zephyr-4000-ultra" {
		t.Fatalf("unknown hardware did not fall through to a slug: %q", got)
	}

	none := gpuNode("gpu-c", map[string]string{labelGPUCount: "1"}, nil, true, false)
	if got := projectKubeNode(none, nil, discardLog).Devices[0].DeviceClass; got != unknownClass {
		t.Fatalf("a node with no product label should be %q, got %q", unknownClass, got)
	}
}
