package fake

import (
	"context"
	"strings"
	"testing"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
)

// TestSeededDevicesMatchClusterRollups is the invariant that makes the fake
// worth testing against: the per-device seed and the per-class rollup on the
// cluster are two views of one truth.
func TestSeededDevicesMatchClusterRollups(t *testing.T) {
	b := newTestBackend(t)
	ctx := context.Background()
	clusters, err := b.Clusters(ctx, api.ListOpts{All: true})
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range clusters {
		devices, err := b.Devices(ctx, api.ListOpts{All: true, Cluster: c.Name})
		if err != nil {
			t.Fatal(err)
		}
		got := map[string]api.DeviceSummary{}
		for _, d := range devices {
			s := got[d.Class]
			s.Total++
			if d.Health == api.HealthHealthy {
				s.Healthy++
			}
			if d.Allocated() {
				s.Allocated++
			}
			got[d.Class] = s
		}
		for _, want := range c.Devices {
			g := got[want.Class]
			if g.Total != want.Total || g.Healthy != want.Healthy || g.Allocated != want.Allocated {
				t.Errorf("%s/%s: devices %+v, cluster rollup %+v", c.Name, want.Class, g, want)
			}
		}
		nodes, err := b.Nodes(ctx, api.ListOpts{All: true, Cluster: c.Name})
		if err != nil {
			t.Fatal(err)
		}
		if len(nodes) != c.Nodes {
			t.Errorf("%s: %d nodes seeded, cluster reports %d", c.Name, len(nodes), c.Nodes)
		}
	}
}

func TestSupplyHidesDisabledClusterByDefault(t *testing.T) {
	b := newTestBackend(t)
	ctx := context.Background()
	nodes, err := b.Nodes(ctx, api.ListOpts{})
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range nodes {
		if n.Cluster == "lab-0" {
			t.Fatalf("node of a disabled cluster returned without All: %s", n.Name)
		}
	}
	all, err := b.Nodes(ctx, api.ListOpts{All: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(all) <= len(nodes) {
		t.Fatalf("All=true returned %d, not more than %d", len(all), len(nodes))
	}
}

func TestDevicesFilterByDomain(t *testing.T) {
	b := newTestBackend(t)
	ctx := context.Background()
	devices, err := b.Devices(ctx, api.ListOpts{Cluster: "iad-1", Domain: "iad-1-n01/nvswitch-0"})
	if err != nil {
		t.Fatal(err)
	}
	if len(devices) != 8 {
		t.Fatalf("domain holds %d devices, want 8", len(devices))
	}
	for _, d := range devices {
		if d.Topology == nil || d.Topology.Kind != api.TopologyNVSwitch {
			t.Fatalf("device outside the requested fabric: %+v", d.Topology)
		}
	}
	byKind, err := b.Devices(ctx, api.ListOpts{Cluster: "sjc-1", Domain: api.TopologyPCIe})
	if err != nil {
		t.Fatal(err)
	}
	if len(byKind) != 32 {
		t.Fatalf("pcie devices = %d, want 32", len(byKind))
	}
}

func TestNodesCordonedAndPoolScoping(t *testing.T) {
	b := newTestBackend(t)
	ctx := context.Background()
	nodes, err := b.Nodes(ctx, api.ListOpts{Cordoned: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(nodes) != 9 {
		t.Fatalf("cordoned nodes = %d, want 9 (one in iad-1, all eight in ams-1)", len(nodes))
	}
	pooled, err := b.Nodes(ctx, api.ListOpts{Pool: "l40s-serving"})
	if err != nil {
		t.Fatal(err)
	}
	if len(pooled) != 4 {
		t.Fatalf("pool scoping returned %d nodes, want 4", len(pooled))
	}
}

func TestPoolMembershipMatchesNodes(t *testing.T) {
	b := newTestBackend(t)
	ctx := context.Background()
	pools, err := b.Pools(ctx, api.ListOpts{All: true})
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range pools {
		nodes, err := b.Nodes(ctx, api.ListOpts{All: true, Pool: p.Name})
		if err != nil {
			t.Fatal(err)
		}
		if len(nodes) != p.Nodes {
			t.Errorf("pool %s declares %d nodes, %d observed", p.Name, p.Nodes, len(nodes))
		}
		if p.Nodes < p.MinNodes || (p.MaxNodes > 0 && p.Nodes > p.MaxNodes) {
			t.Errorf("pool %s membership %d outside bounds %d-%d", p.Name, p.Nodes, p.MinNodes, p.MaxNodes)
		}
	}
}

func TestSupplyUnknownClusterScopeIsNotFound(t *testing.T) {
	b := newTestBackend(t)
	_, err := b.Devices(context.Background(), api.ListOpts{Cluster: "nope"})
	if code := errs.As(err).Code; code != errs.CodeNotFound {
		t.Fatalf("got %v, want NOT_FOUND", code)
	}
}

func TestSupplyAmbiguousPrefix(t *testing.T) {
	b := newTestBackend(t)
	_, err := b.Node(context.Background(), "iad-1-n")
	if code := errs.As(err).Code; code != errs.CodeAmbiguous {
		t.Fatalf("got %v, want AMBIGUOUS", code)
	}
}

func TestDeviceResolvesByUUID(t *testing.T) {
	b := newTestBackend(t)
	ctx := context.Background()
	devices, err := b.Devices(ctx, api.ListOpts{Node: "iad-1-n01"})
	if err != nil {
		t.Fatal(err)
	}
	want := devices[0]
	for _, ref := range []string{want.ID, want.UUID, want.UUID[:12]} {
		got, err := b.Device(ctx, ref)
		if err != nil {
			t.Fatalf("resolve %s: %v", ref, err)
		}
		if got.ID != want.ID {
			t.Errorf("resolve %s gave %s", ref, got.ID)
		}
	}
	if !strings.HasPrefix(want.ID, "dev_") {
		t.Errorf("device id %q lacks the dev_ prefix", want.ID)
	}
}

func TestSupplyInjectedFailure(t *testing.T) {
	t.Setenv("HANGAR_FAKE_FAIL", "CAPACITY_UNAVAILABLE")
	b := newTestBackend(t)
	ctx := context.Background()
	for name, call := range map[string]func() error{
		"nodes":   func() error { _, err := b.Nodes(ctx, api.ListOpts{}); return err },
		"node":    func() error { _, err := b.Node(ctx, "iad-1-n01"); return err },
		"devices": func() error { _, err := b.Devices(ctx, api.ListOpts{}); return err },
		"device":  func() error { _, err := b.Device(ctx, "dev_x"); return err },
		"pools":   func() error { _, err := b.Pools(ctx, api.ListOpts{}); return err },
		"pool":    func() error { _, err := b.Pool(ctx, "h100-prod"); return err },
	} {
		if code := errs.As(call()).Code; code != errs.CodeCapacityUnavailable {
			t.Errorf("%s: got %v, want CAPACITY_UNAVAILABLE", name, code)
		}
	}
}
