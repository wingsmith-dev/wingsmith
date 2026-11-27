package serve

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"syscall"
)

// Grant is the accelerators one replica container holds.
type Grant struct {
	Deployment string   `json:"deployment"`
	GPUs       []string `json:"gpus"`
}

// Allocations is the GPU allocation map, persisted as JSON under an exclusive flock so a second
// wingsmith-host on the same machine cannot hand out the same cards. Keyed by container name.
//
// The containers' own labels carry the same grant, so a lost file is rebuilt from what is running
// (Adopt), and the file is what survives a container being removed behind the host's back.
type Allocations struct {
	path string
	mu   sync.Mutex
	lock *os.File
	m    map[string]Grant
}

// ErrNoCapacity is a replica that needs more free GPUs than the host has.
var ErrNoCapacity = errors.New("not enough free GPUs on this host")

// OpenAllocations loads (or creates) the map at path and holds its lock until Close.
func OpenAllocations(path string) (*Allocations, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	lock, err := os.OpenFile(path+".lock", os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil { //nolint:gosec // fd fits
		_ = lock.Close()
		return nil, fmt.Errorf("%s is held by another wingsmith-host: %w", path, err)
	}
	a := &Allocations{path: path, lock: lock, m: map[string]Grant{}}
	raw, err := os.ReadFile(path)
	switch {
	case errors.Is(err, os.ErrNotExist):
	case err != nil:
		_ = a.Close()
		return nil, err
	default:
		if err := json.Unmarshal(raw, &a.m); err != nil {
			_ = a.Close()
			return nil, fmt.Errorf("allocation map %s: %w", path, err)
		}
	}
	return a, nil
}

// Close releases the lock.
func (a *Allocations) Close() error { return a.lock.Close() }

// Get returns the grant for one container.
func (a *Allocations) Get(container string) (Grant, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	g, ok := a.m[container]
	return g, ok
}

// Allocate grants n of the healthy GPUs nobody holds to container, lowest-listed first, and
// persists the map. A container that already holds a grant of the right size keeps it.
func (a *Allocations) Allocate(container, deployment string, n int, healthy []string) ([]string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if g, ok := a.m[container]; ok && len(g.GPUs) == n {
		return g.GPUs, nil
	}
	held := map[string]bool{}
	for name, g := range a.m {
		if name == container {
			continue
		}
		for _, u := range g.GPUs {
			held[u] = true
		}
	}
	var free []string
	for _, u := range healthy {
		if !held[u] {
			free = append(free, u)
		}
	}
	if len(free) < n {
		return nil, fmt.Errorf("%w: need %d, %d free of %d", ErrNoCapacity, n, len(free), len(healthy))
	}
	grant := append([]string(nil), free[:n]...)
	a.m[container] = Grant{Deployment: deployment, GPUs: grant}
	return grant, a.saveLocked()
}

// Release forgets a container's grant.
func (a *Allocations) Release(container string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if _, ok := a.m[container]; !ok {
		return nil
	}
	delete(a.m, container)
	return a.saveLocked()
}

// Adopt makes the map match the containers that exist: a running container's labelled grant is
// recorded, and a grant whose container is gone is dropped.
func (a *Allocations) Adopt(live map[string]Grant) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	next := make(map[string]Grant, len(live))
	for name, g := range live {
		sort.Strings(g.GPUs)
		next[name] = g
	}
	a.m = next
	return a.saveLocked()
}

func (a *Allocations) saveLocked() error {
	raw, err := json.MarshalIndent(a.m, "", "  ")
	if err != nil {
		return err
	}
	tmp := a.path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, a.path)
}
