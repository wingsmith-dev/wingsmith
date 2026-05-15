package observe

import (
	"fmt"
	"strings"
	stdsync "sync"

	"k8s.io/client-go/discovery"
)

// multinodeBackends are the leader-worker controllers Dynamo can render a multi-node replica
// onto, matched as substrings of an API group name rather than as exact groups.
//
// Substrings because the group a backend serves is the backend's to change, and the cost of the
// two mistakes is not symmetric: reading an unknown group as "no backend" parks every multi-node
// replica on a cluster that could run them, and the reason is invisible from the cluster itself.
// A loose match risks the opposite, which the render then fails loudly on.
var multinodeBackends = []string{"grove", "leaderworkerset"}

// GroupSource reports the API groups a cluster serves.
type GroupSource interface {
	Groups() ([]string, error)
}

// DiscoveryGroups reads them from the API server's discovery endpoint.
//
// Discovery rather than listing CustomResourceDefinitions: `/apis` is served to every
// authenticated caller through the built-in `system:discovery` role, so this costs no RBAC rule
// anywhere. Listing CRDs would need `apiextensions.k8s.io` read across the cluster, which is a
// broad grant to buy one boolean.
type DiscoveryGroups struct {
	Client discovery.DiscoveryInterface
}

var _ GroupSource = (*DiscoveryGroups)(nil)

func (d *DiscoveryGroups) Groups() ([]string, error) {
	if d == nil || d.Client == nil {
		return nil, nil
	}
	groups, err := d.Client.ServerGroups()
	if err != nil {
		return nil, fmt.Errorf("discover api groups: %w", err)
	}
	out := make([]string, 0, len(groups.Groups))
	for _, g := range groups.Groups {
		out = append(out, g.Name)
	}
	return out, nil
}

// multinodeCapable says whether any group served looks like a leader-worker backend.
func multinodeCapable(groups []string) bool {
	for _, group := range groups {
		lowered := strings.ToLower(group)
		for _, backend := range multinodeBackends {
			if strings.Contains(lowered, backend) {
				return true
			}
		}
	}
	return false
}

// Capability is where the observation loop publishes what it detected, for the reconcile loop to
// read. The same shape as DeviceIndex and for the same reason: two loops run on their own clocks
// and one of them holds a fact the other has to act on.
type Capability struct {
	mu        stdsync.RWMutex
	multinode bool
}

// Publish replaces the reading. A tick that could not discover reports false, which parks
// multi-node replicas rather than hanging them.
func (c *Capability) Publish(multinode bool) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.multinode = multinode
}

// Multinode says whether a leader-worker backend was seen on the last tick. A nil holder, or one
// nothing has published to yet, reads false.
func (c *Capability) Multinode() bool {
	if c == nil {
		return false
	}
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.multinode
}
