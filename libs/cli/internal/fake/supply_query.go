package fake

import (
	"context"
	"sort"
	"strings"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
)

// Nodes lists cluster members, applying the filters the plane is expected to
// apply server-side.
func (b *Backend) Nodes(ctx context.Context, opts api.ListOpts) ([]api.Node, error) {
	if err := b.injected(); err != nil {
		return nil, err
	}
	cluster, err := b.scope(ctx, opts)
	if err != nil {
		return nil, err
	}
	var out []api.Node
	for _, n := range b.state.Nodes {
		if !b.visible(opts.All, n.Cluster) {
			continue
		}
		if cluster != "" && n.Cluster != cluster {
			continue
		}
		if !refMatch(opts.Pool, n.PoolID, n.Pool) {
			continue
		}
		if opts.Status != "" && !strings.EqualFold(n.Status, opts.Status) {
			continue
		}
		if opts.Cordoned && !n.Cordoned {
			continue
		}
		if opts.Class != "" && !nodeHasClass(n, opts.Class) {
			continue
		}
		if opts.Domain != "" && !nodeInDomain(n, opts.Domain) {
			continue
		}
		if opts.Health != "" && !strings.EqualFold(nodeHealth(n), opts.Health) {
			continue
		}
		if opts.Search != "" && !containsFold(opts.Search, n.Name, n.ID, n.Cluster, n.Pool, n.Shape) {
			continue
		}
		if !labelsMatch(n.Labels, opts.Labels) {
			continue
		}
		out = append(out, n)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return limit(out, opts.Limit), nil
}

// Node resolves a node by exact ID, exact name, or unique prefix.
func (b *Backend) Node(ctx context.Context, ref string) (api.Node, error) {
	if err := b.injected(); err != nil {
		return api.Node{}, err
	}
	return resolve(ref, "node", b.state.Nodes,
		func(n api.Node) (string, string) { return n.ID, n.Name }, "hgr node ls")
}

// Devices lists individual accelerators.
func (b *Backend) Devices(ctx context.Context, opts api.ListOpts) ([]api.Device, error) {
	if err := b.injected(); err != nil {
		return nil, err
	}
	cluster, err := b.scope(ctx, opts)
	if err != nil {
		return nil, err
	}
	var out []api.Device
	for _, d := range b.state.Devices {
		if !b.visible(opts.All, d.Cluster) {
			continue
		}
		if cluster != "" && d.Cluster != cluster {
			continue
		}
		if !refMatch(opts.Node, d.NodeID, d.Node) {
			continue
		}
		if opts.Pool != "" && !strings.EqualFold(d.Pool, opts.Pool) {
			continue
		}
		if opts.Class != "" && !strings.EqualFold(d.Class, opts.Class) {
			continue
		}
		if opts.Health != "" && !strings.EqualFold(d.Health, opts.Health) {
			continue
		}
		if opts.Status != "" && !strings.EqualFold(d.AllocationStatus(), opts.Status) {
			continue
		}
		if opts.Domain != "" && !deviceInDomain(d, opts.Domain) {
			continue
		}
		if opts.Search != "" && !containsFold(opts.Search, d.ID, d.UUID, d.Node, d.Cluster, d.Class, d.Model, allocationName(d)) {
			continue
		}
		out = append(out, d)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Node != out[j].Node {
			return out[i].Node < out[j].Node
		}
		return out[i].Index < out[j].Index
	})
	return limit(out, opts.Limit), nil
}

// Device resolves a device by exact ID, UUID, or unique prefix of either.
func (b *Backend) Device(ctx context.Context, ref string) (api.Device, error) {
	if err := b.injected(); err != nil {
		return api.Device{}, err
	}
	return resolve(ref, "device", b.state.Devices,
		func(d api.Device) (string, string) { return d.ID, d.UUID }, "hgr device ls")
}

// Pools lists declared provisioning specs with their observed membership.
func (b *Backend) Pools(ctx context.Context, opts api.ListOpts) ([]api.Pool, error) {
	if err := b.injected(); err != nil {
		return nil, err
	}
	cluster, err := b.scope(ctx, opts)
	if err != nil {
		return nil, err
	}
	var out []api.Pool
	for _, p := range b.state.Pools {
		if !opts.All && p.Status == api.PoolDisabled {
			continue
		}
		if !b.visible(opts.All, p.Cluster) {
			continue
		}
		if cluster != "" && p.Cluster != cluster {
			continue
		}
		if opts.Status != "" && !strings.EqualFold(p.Status, opts.Status) {
			continue
		}
		if opts.Class != "" && !strings.EqualFold(p.DeviceClass, opts.Class) {
			continue
		}
		if opts.Domain != "" && !strings.EqualFold(p.TopologyKind, opts.Domain) {
			continue
		}
		if opts.Search != "" && !containsFold(opts.Search, p.Name, p.ID, p.Cluster, p.DeviceClass, p.Source) {
			continue
		}
		if !labelsMatch(p.Labels, opts.Labels) {
			continue
		}
		out = append(out, p)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Cluster != out[j].Cluster {
			return out[i].Cluster < out[j].Cluster
		}
		return out[i].Name < out[j].Name
	})
	return limit(out, opts.Limit), nil
}

// Pool resolves a pool by exact ID, exact name, or unique prefix.
func (b *Backend) Pool(ctx context.Context, ref string) (api.Pool, error) {
	if err := b.injected(); err != nil {
		return api.Pool{}, err
	}
	return resolve(ref, "pool", b.state.Pools,
		func(p api.Pool) (string, string) { return p.ID, p.Name }, "hgr pool ls")
}

// scope resolves --cluster through the same path as `cluster get`, so an
// unknown scope is a NOT_FOUND rather than a silently empty listing.
func (b *Backend) scope(ctx context.Context, opts api.ListOpts) (string, error) {
	if opts.Cluster == "" {
		return "", nil
	}
	c, err := b.Cluster(ctx, opts.Cluster)
	if err != nil {
		return "", err
	}
	return c.Name, nil
}

// visible hides supply belonging to a disabled cluster unless --all, matching
// how `cluster ls` treats the cluster itself.
func (b *Backend) visible(all bool, cluster string) bool {
	if all {
		return true
	}
	for _, c := range b.state.Clusters {
		if c.Name == cluster {
			return c.Phase != api.PhaseDisabled
		}
	}
	return true
}

// resolve is the shared reference resolution every `get` uses: exact id,
// exact secondary key, then unique prefix on either.
func resolve[T any](ref, noun string, items []T, keys func(T) (string, string), listCmd string) (T, error) {
	var zero T
	if strings.TrimSpace(ref) == "" {
		return zero, errs.New(errs.CodeUsage, "a %s name or id is required", noun)
	}
	var hits []T
	var names []string
	for _, it := range items {
		id, name := keys(it)
		if id == ref || name == ref {
			return it, nil
		}
		if strings.HasPrefix(id, ref) || strings.HasPrefix(name, ref) {
			hits = append(hits, it)
			names = append(names, name)
		}
	}
	switch len(hits) {
	case 1:
		return hits[0], nil
	case 0:
		return zero, errs.New(errs.CodeNotFound, "no %s named %q", noun, ref).
			WithFix("List "+noun+"s", listCmd)
	default:
		return zero, errs.New(errs.CodeAmbiguous, "%q matches %d %ss", ref, len(hits), noun).
			WithCause(strings.Join(names, ", "))
	}
}

func refMatch(ref, id, name string) bool {
	if ref == "" {
		return true
	}
	return id == ref || name == ref || strings.HasPrefix(id, ref) || strings.HasPrefix(name, ref)
}

func nodeHasClass(n api.Node, class string) bool {
	for _, d := range n.Devices {
		if strings.EqualFold(d.Class, class) {
			return true
		}
	}
	return false
}

func nodeInDomain(n api.Node, ref string) bool {
	for _, t := range n.Topology {
		if strings.EqualFold(t.Kind, ref) || t.Name == ref || strings.HasPrefix(t.ID, ref) {
			return true
		}
	}
	return false
}

func deviceInDomain(d api.Device, ref string) bool {
	if d.Topology == nil {
		return false
	}
	return strings.EqualFold(d.Topology.Kind, ref) || d.Topology.Name == ref || strings.HasPrefix(d.Topology.ID, ref)
}

// nodeHealth folds the node's device rollup into one word, so `--health` means
// the same thing on a node as it does on a device.
func nodeHealth(n api.Node) string {
	t := n.DeviceTotals()
	switch {
	case t.Total == 0:
		return api.HealthUnknown
	case t.Healthy == t.Total:
		return api.HealthHealthy
	case t.Healthy == 0:
		return api.HealthUnhealthy
	}
	return api.HealthDegraded
}

func allocationName(d api.Device) string {
	if d.Allocation == nil {
		return ""
	}
	return d.Allocation.Deployment
}

func labelsMatch(have, want map[string]string) bool {
	for k, v := range want {
		if have[k] != v {
			return false
		}
	}
	return true
}

func containsFold(needle string, fields ...string) bool {
	needle = strings.ToLower(needle)
	for _, f := range fields {
		if strings.Contains(strings.ToLower(f), needle) {
			return true
		}
	}
	return false
}

func limit[T any](items []T, n int) []T {
	if n > 0 && len(items) > n {
		return items[:n]
	}
	return items
}
