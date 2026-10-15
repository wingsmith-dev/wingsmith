package observe

import (
	"context"
	"fmt"
	"log/slog"
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
	if n.Devices[0].DomainKind != "pcie_root" {
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
	if got := projectKubeNode(labelled, nil, labelPool, discardLog).Devices[0].DeviceClass; got != "bespoke-class" {
		t.Fatalf("label override lost: %q", got)
	}

	unknown := gpuNode("gpu-b", map[string]string{
		labelGPUCount:   "1",
		labelGPUProduct: "NVIDIA-Zephyr-4000-Ultra",
	}, nil, true, false)
	if got := projectKubeNode(unknown, nil, labelPool, discardLog).Devices[0].DeviceClass; got != "zephyr-4000-ultra" {
		t.Fatalf("unknown hardware did not fall through to a slug: %q", got)
	}

	none := gpuNode("gpu-c", map[string]string{labelGPUCount: "1"}, nil, true, false)
	if got := projectKubeNode(none, nil, labelPool, discardLog).Devices[0].DeviceClass; got != unknownClass {
		t.Fatalf("a node with no product label should be %q, got %q", unknownClass, got)
	}
}

// TestKubePoolLabelKeyIsConfigurable: the label a cluster names its pools with is the one
// HANGAR_POOL_LABEL_KEY sets, and observation must read that key rather than a compiled-in one.
// Reading a different key than placement selects on is silent: the node arrives in the default
// pool, no pool of that name is declared, and the plane stores a null pool without complaining.
func TestKubePoolLabelKeyIsConfigurable(t *testing.T) {
	node := gpuNode("gpu-a", map[string]string{
		labelGPUCount:      "1",
		"example.com/pool": "gpu",
	}, nil, true, false)

	d := newKubeDiscoverer(t, node)
	d.PoolLabelKey = "example.com/pool"
	snap, err := d.Discover(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if got := snap.Nodes[0].Pool; got != "gpu" {
		t.Fatalf("pool observed as %q, want %q", got, "gpu")
	}
	if got := snap.Nodes[0].Labels["pool"]; got != "gpu" {
		t.Fatalf("exported pool label is %q, want %q", got, "gpu")
	}

	// Unset falls back to the hangar key, so a cluster that never configured one is unaffected.
	plain := newKubeDiscoverer(t, node)
	if got := plain.poolKey(); got != labelPool {
		t.Fatalf("default pool key is %q, want %q", got, labelPool)
	}
}

// levelRecorder records the levels a logger was asked to emit at.
type levelRecorder struct {
	levels []slog.Level
	msgs   []string
}

func (h *levelRecorder) Enabled(context.Context, slog.Level) bool { return true }

func (h *levelRecorder) Handle(_ context.Context, r slog.Record) error {
	h.levels = append(h.levels, r.Level)
	h.msgs = append(h.msgs, r.Message)
	return nil
}

func (h *levelRecorder) WithAttrs([]slog.Attr) slog.Handler { return h }

func (h *levelRecorder) WithGroup(string) slog.Handler { return h }

func (h *levelRecorder) warnings() []string {
	var out []string
	for i, l := range h.levels {
		if l >= slog.LevelWarn {
			out = append(out, h.msgs[i])
		}
	}
	return out
}

func dcgmInventoryFor(host string, n int) *DCGMInventory {
	devices := make([]DCGMDevice, 0, n)
	for i := 0; i < n; i++ {
		devices = append(devices, DCGMDevice{
			UUID:  fmt.Sprintf("GPU-%s-%d", host, i),
			Index: i,
			Model: "NVIDIA H100 80GB HBM3",
			Host:  host,
		})
	}
	return &DCGMInventory{byHost: map[string][]DCGMDevice{host: devices}, Devices: n, Hosts: 1}
}

// TestMissingGPUCountLabelIsNotAWarning: a node group installed without GFD's labels takes its
// device count from DCGM on every pass for as long as it exists. That is the supported path, so it
// must not shout — a WARN twice every thirty seconds buries the reconcile lines. The count itself
// must still be right.
func TestMissingGPUCountLabelIsNotAWarning(t *testing.T) {
	const name = "ip-10-5-103-180.eu-north-1.compute.internal"
	node := gpuNode(name, map[string]string{}, nil, true, false)
	h := &levelRecorder{}

	got := devicesFor(node, true, dcgmInventoryFor(name, 8), slog.New(h))

	if len(got) != 8 {
		t.Fatalf("observed %d devices, want 8 from the dcgm fallback", len(got))
	}
	if w := h.warnings(); len(w) != 0 {
		t.Fatalf("the dcgm count fallback logged at warn or above: %v", w)
	}
	if len(h.levels) == 0 || h.levels[0] != slog.LevelDebug {
		t.Fatalf("the fallback logged at %v, want it kept at debug for whoever is debugging", h.levels)
	}
}

// TestCountDisagreementStaysAWarning: the fallback going quiet must not take the real signal with
// it. Two sources that both saw the node and returned different numbers is a surprise somebody has
// to resolve, and it keeps its WARN.
func TestCountDisagreementStaysAWarning(t *testing.T) {
	const name = "gpu-h100-a-07"
	node := gpuNode(name, map[string]string{labelGPUCount: "8"}, nil, true, false)
	h := &levelRecorder{}

	if got := devicesFor(node, true, dcgmInventoryFor(name, 6), slog.New(h)); len(got) != 8 {
		t.Fatalf("observed %d devices, want gfd's 8", len(got))
	}
	if w := h.warnings(); len(w) != 1 {
		t.Fatalf("gfd/dcgm disagreement produced warnings %v, want exactly one", w)
	}
}
