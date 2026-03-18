package observe

import (
	"sort"
	"time"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// timestampLayout is the wire's only accepted timestamp form: RFC 3339, UTC, milliseconds.
const timestampLayout = "2006-01-02T15:04:05.000Z"

func formatTime(t time.Time) string { return t.UTC().Format(timestampLayout) }

// projection is one snapshot expressed entirely in contract types. It is what gets compared,
// sent, and re-streamed on resync; the Kubernetes objects it came from do not survive this far.
type projection struct {
	nodes    []wire.NodeObserved
	devices  []wire.DeviceObserved
	domains  []wire.TopologyObserved
	capacity []wire.CapacityObserved
}

// domainAccumulator groups devices into topology domains as the nodes are walked.
//
// A domain is keyed on kind and key alone, never on the node that contributed its first device:
// a fabric spanning machines is one domain seen from several of them, and keying it per node
// would shatter it into one domain per machine.
type domainAccumulator struct {
	order []string
	byKey map[string]*wire.TopologyObserved
	nodes map[string]map[string]bool
}

// addNode records a node as part of a domain, once, in discovery order.
func (a *domainAccumulator) addNode(key, nodeID string) {
	if a.nodes[key] == nil {
		a.nodes[key] = map[string]bool{}
	}
	if a.nodes[key][nodeID] {
		return
	}
	a.nodes[key][nodeID] = true
	dom := a.byKey[key]
	dom.NodeIDs = append(dom.NodeIDs, nodeID)
	dom.SpansNodes = len(dom.NodeIDs) > 1
}

func (o *Observer) project(s Snapshot) projection {
	observedAt := formatTime(s.At)
	acc := domainAccumulator{byKey: map[string]*wire.TopologyObserved{}, nodes: map[string]map[string]bool{}}
	pools := map[string]map[string]*wire.CapacityClass{}
	poolOrder := []string{}

	var p projection
	for _, n := range s.Nodes {
		nodeID := NodeID(o.ClusterID, n.Name)
		deviceIDs := make([]string, 0, len(n.Devices))
		pool := n.Pool
		if pool == "" {
			pool = defaultPool
		}
		if _, ok := pools[pool]; !ok {
			pools[pool] = map[string]*wire.CapacityClass{}
			poolOrder = append(poolOrder, pool)
		}

		allocatedLeft := n.AllocatedDevices
		for _, d := range n.Devices {
			deviceID := DeviceID(o.ClusterID, d.UUID)
			deviceIDs = append(deviceIDs, deviceID)

			var domainID *string
			if d.DomainKind != "" && d.DomainKey != "" {
				id := TopologyDomainID(o.ClusterID, d.DomainKind, d.DomainKey)
				domainID = &id
				key := d.DomainKind + "/" + d.DomainKey
				dom, ok := acc.byKey[key]
				if !ok {
					dom = &wire.TopologyObserved{
						DomainID:   id,
						ClusterID:  o.ClusterID,
						Kind:       d.DomainKind,
						NodeIDs:    []string{},
						DeviceIDs:  []string{},
						ObservedAt: observedAt,
					}
					acc.byKey[key] = dom
					acc.order = append(acc.order, key)
				}
				acc.addNode(key, nodeID)
				dom.DeviceIDs = append(dom.DeviceIDs, deviceID)
			}

			health := d.Health
			if health == "" {
				health = HealthUnknown
			}
			reasons := d.ThrottleReasons
			if reasons == nil {
				reasons = []string{}
			}
			mig := wire.DeviceMig{Enabled: d.Mig.Enabled}
			if d.Mig.Profile != "" {
				profile := d.Mig.Profile
				mig.Profile = &profile
			}
			if d.Mig.InstanceID != "" {
				instance := d.Mig.InstanceID
				mig.InstanceID = &instance
			}
			var alloc *wire.DeviceAllocation
			if d.Allocation != nil {
				alloc = &wire.DeviceAllocation{
					ReplicaID:    d.Allocation.ReplicaID,
					DeploymentID: d.Allocation.DeploymentID,
					ProjectID:    d.Allocation.ProjectID,
				}
			}

			p.devices = append(p.devices, wire.DeviceObserved{
				DeviceID:      deviceID,
				NodeID:        nodeID,
				ClusterID:     o.ClusterID,
				UUID:          d.UUID,
				Index:         d.Index,
				Model:         d.Model,
				DeviceClass:   d.DeviceClass,
				MemoryTotalMB: d.MemoryTotalMB,
				Health:        health,
				Ecc: wire.DeviceEcc{
					ModeEnabled:          d.Ecc.ModeEnabled,
					VolatileCorrected:    d.Ecc.VolatileCorrected,
					VolatileUncorrected:  d.Ecc.VolatileUncorrected,
					AggregateUncorrected: d.Ecc.AggregateUncorrected,
				},
				Throttled:        d.Throttled,
				ThrottleReasons:  reasons,
				Mig:              mig,
				TopologyDomainID: domainID,
				Allocation:       alloc,
				ObservedAt:       observedAt,
			})

			class := d.DeviceClass
			if class == "" {
				class = unknownClass
			}
			row, ok := pools[pool][class]
			if !ok {
				row = &wire.CapacityClass{DeviceClass: class}
				pools[pool][class] = row
			}
			row.Total++
			switch {
			case health != HealthHealthy:
				row.Unhealthy++
			case d.Allocation != nil:
				row.Allocated++
			case allocatedLeft > 0:
				allocatedLeft--
				row.Allocated++
			default:
				row.Free++
			}
		}

		conditions := make([]wire.NodeCondition, 0, len(n.Conditions))
		for _, c := range n.Conditions {
			conditions = append(conditions, wire.NodeCondition{
				Type:    c.Type,
				Status:  c.Status,
				Reason:  c.Reason,
				Message: c.Message,
				Since:   formatTime(c.Since),
			})
		}
		labels := n.Labels
		if labels == nil {
			labels = map[string]string{}
		}
		p.nodes = append(p.nodes, wire.NodeObserved{
			NodeID:         nodeID,
			ClusterID:      o.ClusterID,
			Name:           n.Name,
			Pool:           pool,
			Membership:     membershipJoined,
			Ready:          n.Ready,
			Cordoned:       n.Cordoned,
			Conditions:     conditions,
			KernelVersion:  n.KernelVersion,
			DriverVersion:  n.DriverVersion,
			RuntimeVersion: n.RuntimeVersion,
			AgentVersion:   o.AgentVersion,
			DeviceIDs:      deviceIDs,
			Labels:         labels,
			ObservedAt:     observedAt,
		})
	}

	for _, key := range acc.order {
		p.domains = append(p.domains, *acc.byKey[key])
	}
	for _, pool := range poolOrder {
		classes := make([]wire.CapacityClass, 0, len(pools[pool]))
		for _, row := range pools[pool] {
			classes = append(classes, *row)
		}
		sort.Slice(classes, func(i, j int) bool { return classes[i].DeviceClass < classes[j].DeviceClass })
		p.capacity = append(p.capacity, wire.CapacityObserved{
			ClusterID:  o.ClusterID,
			Pool:       pool,
			Classes:    classes,
			ObservedAt: observedAt,
		})
	}
	return p
}
