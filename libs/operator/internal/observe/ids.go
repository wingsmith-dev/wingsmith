package observe

import (
	"crypto/sha256"

	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
)

// Id prefixes for the objects this cluster observes and therefore names (ADR 0002).
const (
	nodePrefix     = "nod_"
	devicePrefix   = "dev_"
	topologyPrefix = "tpd_"
	workloadPrefix = "dsw_"
)

// mintID derives a stable observed id from a cluster-local natural key.
//
// The body is the first 16 bytes of SHA-256 over the cluster id, the object kind and the natural
// key, rendered with the same Crockford base32 encoder that produces frame ULIDs — so the id has
// the shape the contract demands while being a pure function of what was observed. Nothing is
// persisted and nothing has to be: a restarted operator, or a second replica after a leader
// change, re-derives byte-identical ids from the same cluster. A random ULID would need a durable
// side table to achieve that, and a side table that disagrees with the cluster is a second
// identity to keep in agreement — exactly what ADR 0002 rejects.
func mintID(prefix, clusterID, kind, key string) string {
	h := sha256.New()
	h.Write([]byte(clusterID))
	h.Write([]byte{0})
	h.Write([]byte(kind))
	h.Write([]byte{0})
	h.Write([]byte(key))
	var raw [16]byte
	copy(raw[:], h.Sum(nil))
	return prefix + syncpkg.ULIDBody(raw)
}

// NodeID is the id this cluster gives a node. The natural key is the node's name, which is unique
// within a cluster and survives reboots.
func NodeID(clusterID, name string) string {
	return mintID(nodePrefix, clusterID, "node", name)
}

// DeviceID is the id this cluster gives an accelerator. The natural key is the device UUID, which
// follows the hardware rather than the node it currently sits in.
func DeviceID(clusterID, uuid string) string {
	return mintID(devicePrefix, clusterID, "device", uuid)
}

// TopologyDomainID is the id this cluster gives a topology domain, keyed by kind and identifier.
func TopologyDomainID(clusterID, kind, identifier string) string {
	return mintID(topologyPrefix, clusterID, "topology:"+kind, identifier)
}

// DiscoveredWorkloadID is the id this cluster gives a workload it found already running. The
// natural key is namespace, kind and name, which is what uniquely identifies the object inside its
// own cluster and what it will still be called after the operator restarts.
//
// Stability matters more here than for hardware: a decision to track a workload is stored against
// this id, so an id that changed between operator restarts would ask an operator to re-adopt the
// same five models every time a pod moved.
func DiscoveredWorkloadID(clusterID, namespace, kind, name string) string {
	return mintID(workloadPrefix, clusterID, "workload:"+kind, namespace+"/"+name)
}
