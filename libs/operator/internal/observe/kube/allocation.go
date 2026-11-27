package kube

import (
	"slices"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/observe"
)

// podDevicesFrom reads the pod bindings out of one DCGM scrape.
//
// Cards with no binding are skipped, not recorded against an empty pod: an idle accelerator
// is held by nobody, and grouping the idle ones together under "" would read as one workload
// holding every free card in the cluster.
func podDevicesFrom(inventory *DCGMInventory) observe.PodDevices {
	if inventory == nil {
		return nil
	}
	out := observe.PodDevices{}
	for _, list := range inventory.byHost {
		for _, d := range list {
			if d.Pod == "" || d.Namespace == "" || d.UUID == "" {
				continue
			}
			key := observe.PodKey(d.Namespace, d.Pod)
			if !slices.Contains(out[key], d.UUID) {
				out[key] = append(out[key], d.UUID)
			}
		}
	}
	if len(out) == 0 {
		return nil
	}
	for key := range out {
		slices.Sort(out[key])
	}
	return out
}
