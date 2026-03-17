package fake

import (
	"fmt"
	"hash/fnv"
	"time"

	"github.com/hangar/cli/internal/api"
)

// poolSpec describes one declared pool and the membership the operator is
// pretending to observe for it. The seeded devices are generated from these
// specs so per-class rollups on the cluster always agree with the individual
// devices underneath, the way a real sync stream would.
type poolSpec struct {
	cluster   string
	name      string
	status    string
	class     string
	model     string
	memoryGB  int
	nodes     int
	firstNode int
	perNode   int
	min       int
	max       int
	shape     string
	source    string
	topology  string
	cpuCores  int
	memNodeGB int
	driver    string
	unhealthy int
	allocated int
	mig       string
	cordoned  []int
	notReady  []int
	labels    map[string]string
	taints    []string
	deploys   []string
}

var poolSpecs = []poolSpec{
	{
		cluster: "iad-1", name: "h100-prod", status: api.PoolActive,
		class: "h100-80", model: "NVIDIA H100 80GB SXM5", memoryGB: 80,
		nodes: 24, perNode: 8, min: 16, max: 32,
		shape: "8xh100-80-sxm", source: "managed-metal", topology: api.TopologyNVSwitch,
		cpuCores: 192, memNodeGB: 2048, driver: "550.90.07",
		unhealthy: 2, allocated: 168, mig: "3g.40gb",
		cordoned: []int{7},
		labels:   map[string]string{"tier": "prod", "fabric": "nvswitch"},
		deploys:  []string{"llama-3-70b", "mixtral-8x22b", "qwen2-72b", "embed-bge-m3"},
	},
	{
		cluster: "sjc-1", name: "h200-prod", status: api.PoolActive,
		class: "h200-141", model: "NVIDIA H200 141GB SXM5", memoryGB: 141,
		nodes: 12, perNode: 8, min: 8, max: 24,
		shape: "8xh200-141-sxm", source: "managed-metal", topology: api.TopologyNVLink,
		cpuCores: 224, memNodeGB: 2304, driver: "550.90.07",
		allocated: 40,
		labels:    map[string]string{"tier": "prod"},
		deploys:   []string{"llama-3-405b", "deepseek-v3"},
	},
	{
		cluster: "sjc-1", name: "l40s-serving", status: api.PoolActive,
		class: "l40s-48", model: "NVIDIA L40S 48GB", memoryGB: 48,
		nodes: 4, firstNode: 12, perNode: 8, min: 2, max: 8,
		shape: "8xl40s-48-pcie", source: "managed-metal", topology: api.TopologyPCIe,
		cpuCores: 96, memNodeGB: 1024, driver: "550.90.07",
		unhealthy: 1, allocated: 12,
		labels:  map[string]string{"tier": "prod", "workload": "serving"},
		taints:  []string{"serving=true:NoSchedule"},
		deploys: []string{"sdxl-turbo", "whisper-large-v3"},
	},
	{
		cluster: "ams-1", name: "a100-eu", status: api.PoolPaused,
		class: "a100-80", model: "NVIDIA A100 80GB SXM4", memoryGB: 80,
		nodes: 8, perNode: 8, min: 0, max: 8,
		shape: "8xa100-80-sxm", source: "managed-metal", topology: api.TopologyNVLink,
		cpuCores: 128, memNodeGB: 2048, driver: "535.183.01",
		unhealthy: 4, allocated: 8,
		cordoned: []int{0, 1, 2, 3, 4, 5, 6, 7}, notReady: []int{5},
		labels:  map[string]string{"tier": "prod", "residency": "eu"},
		deploys: []string{"mistral-7b-eu"},
	},
	{
		cluster: "sin-1", name: "h100-sin", status: api.PoolPending,
		class: "h100-80", model: "NVIDIA H100 80GB SXM5", memoryGB: 80,
		nodes: 0, perNode: 8, min: 0, max: 8,
		shape: "8xh100-80-sxm", source: "cloud-reserved", topology: api.TopologyNVSwitch,
		cpuCores: 192, memNodeGB: 2048, driver: "550.90.07",
		labels: map[string]string{"tier": "staging"},
	},
	{
		cluster: "lab-0", name: "lab-l4", status: api.PoolDisabled,
		class: "l4-24", model: "NVIDIA L4 24GB", memoryGB: 24,
		nodes: 2, perNode: 4, min: 0, max: 4,
		shape: "4xl4-24-pcie", source: "managed-metal", topology: api.TopologyPCIe,
		cpuCores: 48, memNodeGB: 384, driver: "535.183.01",
		labels: map[string]string{"tier": "lab"},
	},
}

// buildSupply expands the pool specs into pools, nodes, and devices anchored
// to the seeded clusters.
func buildSupply(now time.Time, clusters []api.Cluster) ([]api.Pool, []api.Node, []api.Device) {
	byName := map[string]api.Cluster{}
	for _, c := range clusters {
		byName[c.Name] = c
	}

	var pools []api.Pool
	var nodes []api.Node
	var devices []api.Device

	for _, sp := range poolSpecs {
		cl := byName[sp.cluster]
		total := sp.nodes * sp.perNode
		pool := api.Pool{
			ID:           mint("pol", sp.cluster+"/"+sp.name),
			Name:         sp.name,
			ClusterID:    cl.ID,
			Cluster:      cl.Name,
			Status:       sp.status,
			DeviceClass:  sp.class,
			DevicesPer:   sp.perNode,
			Shape:        sp.shape,
			MinNodes:     sp.min,
			MaxNodes:     sp.max,
			Source:       sp.source,
			Labels:       sp.labels,
			Taints:       sp.taints,
			Nodes:        sp.nodes,
			TopologyKind: sp.topology,
			Devices:      api.DeviceSummary{Class: sp.class, Total: total, Healthy: total - sp.unhealthy, Allocated: sp.allocated},
			CreatedAt:    cl.EnrolledAt.Add(10 * time.Minute),
			UpdatedAt:    ptr(now.Add(-time.Duration(len(sp.name)) * time.Hour)),
		}

		badFrom := total - sp.unhealthy
		allocated := 0
		ready := 0

		for n := 0; n < sp.nodes; n++ {
			nodeName := fmt.Sprintf("%s-n%02d", sp.cluster, sp.firstNode+n+1)
			nodeID := mint("nod", nodeName)
			domain := api.TopologyDomainRef{
				ID:      mint("tpd", nodeName+"/0"),
				Name:    fmt.Sprintf("%s/%s-0", nodeName, sp.topology),
				Kind:    sp.topology,
				Devices: sp.perNode,
			}

			status := api.NodeReady
			if contains(sp.notReady, n) {
				status = api.NodeNotReady
			}
			if status == api.NodeReady {
				ready++
			}

			nodeRollup := api.DeviceSummary{Class: sp.class, Total: sp.perNode}
			for i := 0; i < sp.perNode; i++ {
				ordinal := n*sp.perNode + i
				devName := fmt.Sprintf("%s/%d", nodeName, i)
				d := api.Device{
					ID:         mint("dev", devName),
					UUID:       gpuUUID(devName),
					Index:      i,
					ClusterID:  cl.ID,
					Cluster:    cl.Name,
					NodeID:     nodeID,
					Node:       nodeName,
					Pool:       sp.name,
					Class:      sp.class,
					Model:      sp.model,
					MemoryGB:   sp.memoryGB,
					Health:     api.HealthHealthy,
					Driver:     sp.driver,
					Topology:   &domain,
					LastSeenAt: cl.LastSyncAt,
				}
				if sp.mig != "" && n == sp.nodes-1 {
					d.MIGEnabled = true
					d.MIGProfile = sp.mig
				}
				switch {
				case ordinal >= badFrom && ordinal%2 == 0:
					d.Health = api.HealthDegraded
					d.HealthReason = "sustained power capping"
					d.ECCCorrected = 118 + ordinal
					d.Throttled = true
					d.ThrottleReasons = []string{"sw_power_cap", "hw_thermal_slowdown"}
				case ordinal >= badFrom:
					d.Health = api.HealthUnhealthy
					d.HealthReason = "uncorrectable ECC errors, device excluded from scheduling"
					d.ECCCorrected = 402
					d.ECCUncorrected = 3
				}
				if d.Health == api.HealthHealthy {
					nodeRollup.Healthy++
					if allocated < sp.allocated {
						dep := sp.deploys[allocated%maxInt(len(sp.deploys), 1)]
						d.Allocation = &api.DeviceAllocation{
							Deployment: dep,
							Replica:    fmt.Sprintf("%s-%d", dep, allocated%7),
							Project:    "default",
							Since:      now.Add(-time.Duration(3+allocated%40) * time.Hour),
						}
						d.MemoryUsedGB = sp.memoryGB * (70 + allocated%20) / 100
						allocated++
						nodeRollup.Allocated++
					}
				}
				devices = append(devices, d)
			}

			node := api.Node{
				ID:         nodeID,
				Name:       nodeName,
				ClusterID:  cl.ID,
				Cluster:    cl.Name,
				Status:     status,
				Cordoned:   contains(sp.cordoned, n),
				Pool:       sp.name,
				PoolID:     pool.ID,
				Shape:      sp.shape,
				CPUCores:   sp.cpuCores,
				MemoryGB:   sp.memNodeGB,
				Kernel:     "5.15.0-119-generic",
				Driver:     sp.driver,
				Devices:    []api.DeviceSummary{nodeRollup},
				Topology:   []api.TopologyDomainRef{domain},
				Labels:     sp.labels,
				JoinedAt:   cl.EnrolledAt.Add(time.Duration(n+3) * time.Hour),
				LastSeenAt: cl.LastSyncAt,
			}
			node.Conditions = conditions(node, nodeRollup)
			nodes = append(nodes, node)
		}

		pool.ReadyNodes = ready
		pools = append(pools, pool)
	}
	return pools, nodes, devices
}

func conditions(n api.Node, rollup api.DeviceSummary) []api.NodeCondition {
	out := []api.NodeCondition{{Type: "Ready", Status: "True"}}
	if n.Status != api.NodeReady {
		out = []api.NodeCondition{{Type: "Ready", Status: "False", Reason: "operator heartbeat missing"}}
	}
	if rollup.Healthy < rollup.Total && n.Status == api.NodeReady {
		out = append(out, api.NodeCondition{
			Type: "DevicesHealthy", Status: "False",
			Reason: fmt.Sprintf("%d of %d devices unhealthy", rollup.Total-rollup.Healthy, rollup.Total),
		})
	} else if n.Status == api.NodeReady {
		out = append(out, api.NodeCondition{Type: "DevicesHealthy", Status: "True"})
	}
	if n.Cordoned {
		out = append(out, api.NodeCondition{Type: "Schedulable", Status: "False", Reason: "cordoned"})
	}
	return out
}

// mint derives a stable, prefixed identifier from a natural key so fixtures
// and prefix resolution stay reproducible across runs.
func mint(prefix, key string) string {
	h := fnv.New64a()
	_, _ = h.Write([]byte(prefix + ":" + key))
	return fmt.Sprintf("%s_%011x", prefix, h.Sum64()&0xffffffffffff)
}

func gpuUUID(key string) string {
	h := fnv.New64a()
	_, _ = h.Write([]byte("uuid/" + key))
	v := h.Sum64()
	return fmt.Sprintf("GPU-%08x-%04x-%04x-%04x-%012x",
		uint32(v>>32), uint16(v>>16), uint16(v), uint16(v>>48), v&0xffffffffffff)
}

func contains(v []int, n int) bool {
	for _, x := range v {
		if x == n {
			return true
		}
	}
	return false
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
