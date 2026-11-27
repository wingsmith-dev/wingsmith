package observe

import (
	"sync"
)

// PodDevices is which accelerators each pod holds, keyed by the pod's namespaced name.
//
// The values are device UUIDs instead of this cluster's minted device ids, because the UUID is
// what the exporter reports and what `DeviceID` is derived from. Minting is left to the reader, so
// that a caller holding a cluster id does it once in the place it already names things.
type PodDevices map[string][]string

// PodKey is how a pod is named in a PodDevices map.
func PodKey(namespace, name string) string {
	return namespace + "/" + name
}

// DeviceIndex is the most recent pod-to-accelerator binding, shared between the loop that reads it
// and the loop that needs it.
//
// The observation loop scrapes the exporters on its own tick; the reconcile loop needs the same
// answer to say which cards a replica holds, and scraping a second time would double the load on
// every exporter in the cluster and still give two readings that disagree. One reading, published
// once, read by both.
//
// A reader that finds nothing gets nothing, never a guess. An empty index means the exporter was
// unreachable or reported no bindings, and reporting an allocation with no devices would tell the
// plane a replica holds zero cards, which is a claim, and a false one.
type DeviceIndex struct {
	mu      sync.RWMutex
	devices PodDevices
}

// Publish replaces the binding with a fresh reading. A nil or empty reading clears it, because a
// scrape that found nothing is not evidence that the previous one is still true.
func (i *DeviceIndex) Publish(devices PodDevices) {
	if i == nil {
		return
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	i.devices = devices
}

// DevicesForPod is the UUIDs of the accelerators one pod holds, or nil if the binding is unknown.
func (i *DeviceIndex) DevicesForPod(namespace, name string) []string {
	if i == nil {
		return nil
	}
	i.mu.RLock()
	defer i.mu.RUnlock()
	found := i.devices[PodKey(namespace, name)]
	if len(found) == 0 {
		return nil
	}
	return append([]string(nil), found...)
}
