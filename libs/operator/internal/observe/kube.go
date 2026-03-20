package observe

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"
	"unicode"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Labels and annotations the discoverer reads. GPU inventory in a real cluster is published by
// the NVIDIA device plugin's GPU feature discovery and by NFD; nothing here talks to a driver, so
// the operator needs no GPU and no privileged access to describe one.
const (
	labelGPUCount     = "nvidia.com/gpu.count"
	labelGPUProduct   = "nvidia.com/gpu.product"
	labelGPUMemory    = "nvidia.com/gpu.memory"
	labelGPUClique    = "nvidia.com/gpu.clique"
	labelMigStrategy  = "nvidia.com/mig.strategy"
	labelDriverFull   = "nvidia.com/cuda.driver-version.full"
	labelDriverLegacy = "nvidia.com/gpu.driver-version"
	labelRegion       = "topology.kubernetes.io/region"
	labelZone         = "topology.kubernetes.io/zone"
	labelPool         = "hangar.dev/pool"
	labelDeviceClass  = "hangar.dev/device-class"

	// annotationDeviceUUIDs carries the node's accelerator UUIDs in index order. Device UUIDs
	// are not published as labels — a label value cannot hold eight of them — so a node agent
	// or a bootstrap job writes them here. Absent it, devices are keyed by node and index,
	// which is stable for as long as the hardware does not move between machines.
	annotationDeviceUUIDs = "hangar.dev/device-uuids"

	// annotationUnhealthy lists the indices or UUIDs of accelerators a health checker has
	// condemned.
	annotationUnhealthy = "hangar.dev/unhealthy-devices"

	resourceGPU = corev1.ResourceName("nvidia.com/gpu")
)

// KubeDiscoverer reads supply out of the Kubernetes API.
//
// It is deliberately uncached and read-only: the observation loop runs on an interval, so a watch
// would buy latency the hub does not need and a second copy of every node object it does not want.
type KubeDiscoverer struct {
	Client client.Client
}

var _ Discoverer = (*KubeDiscoverer)(nil)

// +kubebuilder:rbac:groups="",resources=nodes,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch

// Discover implements Discoverer. KubeDiscoverer is not a controller-runtime reconciler, but the
// RBAC markers live here rather than on a Reconcile method because this is the only place that
// issues the List calls they authorize; controller-gen aggregates markers by package, not by
// type, so placement next to the call site keeps the grant traceable to its use.
func (k *KubeDiscoverer) Discover(ctx context.Context) (Snapshot, error) {
	var nodes corev1.NodeList
	if err := k.Client.List(ctx, &nodes); err != nil {
		return Snapshot{}, fmt.Errorf("list nodes: %w", err)
	}
	var pods corev1.PodList
	if err := k.Client.List(ctx, &pods); err != nil {
		return Snapshot{}, fmt.Errorf("list pods: %w", err)
	}
	workloads, err := k.discoverWorkloads(ctx, &pods)
	if err != nil {
		return Snapshot{}, err
	}
	allocated := allocatedByNode(&pods)

	out := Snapshot{At: time.Now().UTC(), Nodes: make([]Node, 0, len(nodes.Items)), Workloads: workloads}
	for i := range nodes.Items {
		n := projectKubeNode(&nodes.Items[i])
		n.AllocatedDevices = allocated[n.Name]
		out.Nodes = append(out.Nodes, n)
	}
	return out, nil
}

// allocatedByNode counts accelerators committed to work, from the cluster's own scheduling
// records rather than from the hardware. It takes the pod listing rather than making its own:
// workload discovery reads the same pods, and two listings a moment apart would let a node's
// allocation and the workload holding it disagree.
func allocatedByNode(pods *corev1.PodList) map[string]int {
	out := map[string]int{}
	for i := range pods.Items {
		p := &pods.Items[i]
		if !schedulable(p) {
			continue
		}
		if devices := podDevices(p); devices > 0 {
			out[p.Spec.NodeName] += devices
		}
	}
	return out
}

func projectKubeNode(n *corev1.Node) Node {
	labels := n.Labels
	out := Node{
		Name:           n.Name,
		Pool:           labels[labelPool],
		Cordoned:       n.Spec.Unschedulable,
		KernelVersion:  n.Status.NodeInfo.KernelVersion,
		RuntimeVersion: n.Status.NodeInfo.ContainerRuntimeVersion,
		DriverVersion:  firstNonEmpty(labels[labelDriverFull], labels[labelDriverLegacy]),
		Labels:         exportedLabels(labels),
	}
	for _, c := range n.Status.Conditions {
		if c.Type == corev1.NodeReady {
			out.Ready = c.Status == corev1.ConditionTrue
		}
		out.Conditions = append(out.Conditions, Condition{
			Type:    snake(string(c.Type)),
			Status:  strings.ToLower(string(c.Status)),
			Reason:  snake(c.Reason),
			Message: c.Message,
			Since:   c.LastTransitionTime.Time,
		})
	}
	out.Devices = devicesFor(n, out.Ready)
	return out
}

func devicesFor(n *corev1.Node, ready bool) []Device {
	labels, annotations := n.Labels, n.Annotations
	count := atoi(labels[labelGPUCount])
	if count <= 0 {
		return nil
	}
	uuids := splitList(annotations[annotationDeviceUUIDs])
	unhealthy := map[string]struct{}{}
	for _, u := range splitList(annotations[annotationUnhealthy]) {
		unhealthy[u] = struct{}{}
	}

	model := labels[labelGPUProduct]
	class := labels[labelDeviceClass]
	if class == "" {
		class = deviceClass(model)
	}
	memory := int64(atoi(labels[labelGPUMemory]))
	mig := Mig{Enabled: labels[labelMigStrategy] != "" && labels[labelMigStrategy] != "none"}
	clique := labels[labelGPUClique]

	devices := make([]Device, 0, count)
	for i := 0; i < count; i++ {
		uuid := fmt.Sprintf("%s-gpu%d", n.Name, i)
		if i < len(uuids) && uuids[i] != "" {
			uuid = uuids[i]
		}
		health := HealthHealthy
		switch {
		case !ready:
			health = HealthUnknown
		default:
			if _, bad := unhealthy[uuid]; bad {
				health = HealthUnhealthy
			}
			if _, bad := unhealthy[strconv.Itoa(i)]; bad {
				health = HealthUnhealthy
			}
		}
		d := Device{
			UUID:            uuid,
			Index:           i,
			Model:           model,
			DeviceClass:     class,
			MemoryTotalMB:   memory,
			Health:          health,
			ThrottleReasons: []string{},
			Mig:             mig,
		}
		if clique != "" {
			d.DomainKind, d.DomainKey = "nvlink", clique
		} else {
			d.DomainKind, d.DomainKey = "pcie", n.Name
		}
		devices = append(devices, d)
	}
	return devices
}

// exportedLabels narrows a node's labels to the handful the plane places against. A node carries
// dozens of them and the observed object is not a mirror of Kubernetes.
func exportedLabels(in map[string]string) map[string]string {
	out := map[string]string{}
	if v := in[labelRegion]; v != "" {
		out["region"] = v
	}
	if v := in[labelZone]; v != "" {
		out["zone"] = v
	}
	if in[labelGPUClique] != "" {
		out["fabric"] = "nvlink"
	}
	for k, v := range in {
		if rest, ok := strings.CutPrefix(k, "hangar.dev/"); ok {
			out[rest] = v
		}
	}
	return out
}

// deviceClass derives a placement class from the marketing name the device plugin publishes, so
// that a cluster with no hangar.dev/device-class label still groups its hardware.
func deviceClass(product string) string {
	if product == "" {
		return unknownClass
	}
	s := strings.ToLower(product)
	s = strings.ReplaceAll(s, "nvidia-", "")
	s = strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			return r
		}
		return '-'
	}, s)
	for strings.Contains(s, "--") {
		s = strings.ReplaceAll(s, "--", "-")
	}
	return strings.Trim(s, "-")
}

// snake renders a Kubernetes CamelCase identifier in the wire's lower_snake_case vocabulary.
// An acronym stays together — PIDPressure is pid_pressure, not p_i_d_pressure.
func snake(s string) string {
	runes := []rune(s)
	var b strings.Builder
	for i, r := range runes {
		switch {
		case r == '-' || r == ' ' || r == '.' || r == '/':
			b.WriteRune('_')
		case unicode.IsUpper(r):
			prevLower := i > 0 && (unicode.IsLower(runes[i-1]) || unicode.IsDigit(runes[i-1]))
			nextLower := i+1 < len(runes) && unicode.IsLower(runes[i+1])
			if i > 0 && (prevLower || (unicode.IsUpper(runes[i-1]) && nextLower)) {
				b.WriteRune('_')
			}
			b.WriteRune(unicode.ToLower(r))
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}

func splitList(s string) []string {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	for i := range parts {
		parts[i] = strings.TrimSpace(parts[i])
	}
	return parts
}

func atoi(s string) int {
	n, err := strconv.Atoi(strings.TrimSpace(s))
	if err != nil {
		return 0
	}
	return n
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}
