package observe

import (
	stdsync "sync"
)

// Capability is where the observation loop publishes what it detected, for the reconcile loop to
// read. The same shape as DeviceIndex and for the same reason: two loops run on their own clocks
// and one of them holds a fact the other has to act on.
type Capability struct {
	mu        stdsync.RWMutex
	multinode bool
}

// Publish replaces the reading. A tick that could not discover reports false, which parks
// multi-node replicas instead of hanging them.
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
