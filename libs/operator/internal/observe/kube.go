package observe

import (
	"context"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"
	"unicode"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/devices"
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
	// are not published as labels — a label value cannot hold eight of them — and Kubernetes
	// does not expose them at all, so identity comes from the DCGM exporter (ADR 0007). This
	// annotation is retained as an override for a cluster that runs no DCGM; absent both,
	// devices are keyed by node and index, which is stable for as long as the hardware does
	// not move between machines.
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
	// DCGM is the in-cluster exporter device identity is read from. Nil, or unreachable, and
	// discovery degrades to the annotation and then to synthetic ids rather than failing:
	// inventory the plane cannot see is worse than inventory whose metrics do not join.
	DCGM *DCGMSource
	Log  *slog.Logger
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

	inventory, err := k.DCGM.Scrape(ctx)
	if err != nil {
		k.log().Warn("dcgm exporter unreachable; falling back to annotated or synthetic device ids",
			"endpoint", k.DCGM.Endpoint, "error", err)
	} else if inventory != nil {
		k.log().Debug("scraped device identity", "devices", inventory.Devices, "hosts", inventory.Hosts)
	}

	out := Snapshot{At: time.Now().UTC(), Nodes: make([]Node, 0, len(nodes.Items)), Workloads: workloads}
	for i := range nodes.Items {
		n := projectKubeNode(&nodes.Items[i], inventory, k.log())
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

func (k *KubeDiscoverer) log() *slog.Logger {
	if k.Log == nil {
		return slog.Default()
	}
	return k.Log
}

func projectKubeNode(n *corev1.Node, inventory *DCGMInventory, log *slog.Logger) Node {
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
	out.Devices = devicesFor(n, out.Ready, inventory, log)
	return out
}

// devicesFor enumerates a node's accelerators from two views that are each authoritative about a
// different thing.
//
// GPU Feature Discovery decides how many there are. That is the count the kubelet advertises and
// the scheduler allocates against, so a device the plane can place on is by definition one GFD
// counted; DCGM sees the hardware, which is a different question. DCGM decides what each one is
// called, because the true UUID is the only key device-scoped telemetry can be joined on.
//
// When the two disagree on the count, GFD wins and the disagreement is logged with both numbers:
// DCGM entries beyond the GFD count are dropped, and GFD slots DCGM did not name fall through to
// the annotation and then to a synthetic id. Neither view is discarded silently. If GFD says
// nothing at all — no gpu.count label — DCGM's count is used, so a cluster without GFD still
// yields inventory.
func devicesFor(n *corev1.Node, ready bool, inventory *DCGMInventory, log *slog.Logger) []Device {
	labels, annotations := n.Labels, n.Annotations
	scraped := inventory.forNode(n.Name, labels)
	byIndex := make(map[int]DCGMDevice, len(scraped))
	for _, d := range scraped {
		if d.Index >= 0 {
			byIndex[d.Index] = d
		}
	}

	count := atoi(labels[labelGPUCount])
	switch {
	case count <= 0 && len(scraped) > 0:
		log.Warn("node advertises no gpu.count label; taking the device count from dcgm",
			"node", n.Name, "dcgm", len(scraped))
		count = len(scraped)
	case count <= 0:
		return nil
	case len(scraped) > 0 && len(scraped) != count:
		log.Warn("gfd and dcgm disagree on this node's device count; gfd wins",
			"node", n.Name, "gfd", count, "dcgm", len(scraped))
	}

	uuids := splitList(annotations[annotationDeviceUUIDs])
	unhealthy := map[string]struct{}{}
	for _, u := range splitList(annotations[annotationUnhealthy]) {
		unhealthy[u] = struct{}{}
	}

	model := labels[labelGPUProduct]
	memory := int64(atoi(labels[labelGPUMemory]))
	mig := Mig{Enabled: labels[labelMigStrategy] != "" && labels[labelMigStrategy] != "none"}
	clique := labels[labelGPUClique]

	devices := make([]Device, 0, count)
	for i := 0; i < count; i++ {
		uuid := fmt.Sprintf("%s-gpu%d", n.Name, i)
		if i < len(uuids) && uuids[i] != "" {
			uuid = uuids[i]
		}
		scrapedModel := ""
		if d, ok := byIndex[i]; ok {
			uuid, scrapedModel = d.UUID, d.Model
		}
		nodeModel := model
		if nodeModel == "" {
			nodeModel = scrapedModel
		}
		class := labels[labelDeviceClass]
		if class == "" {
			class = deviceClass(model, scrapedModel)
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
			Model:           nodeModel,
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

// deviceClass names the hardware the way hangar names it, from contract/devices.json. The names
// GFD and DCGM publish are two dialects for the same card and neither is canonical, so the
// dictionary decides; hardware it does not list falls back to a slug of the vendor string, which
// is stable and deterministic and gets logged so the table can be grown.
// The two dialects are tried in the order they are trusted: the GFD label is a node fact that is
// there whether or not the exporter answered, and DCGM's modelName is the same card spelled the
// other way for a cluster where GFD is silent.
func deviceClass(gfdProduct, dcgmModel string) string {
	switch {
	case gfdProduct != "":
		return devices.Load().Resolve(devices.DialectGFD, gfdProduct)
	case dcgmModel != "":
		return devices.Load().Resolve(devices.DialectDCGM, dcgmModel)
	default:
		return unknownClass
	}
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
