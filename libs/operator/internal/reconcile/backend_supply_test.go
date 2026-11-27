package reconcile

import (
	"context"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/backend"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

type stubSupply struct{ inv backend.Inventory }

func (s stubSupply) Inventory(context.Context) (backend.Inventory, error) { return s.inv, nil }

func TestBackendInventoryUsesReconcilerSupply(t *testing.T) {
	r := &SyncedResourceReconciler{Supply: stubSupply{backend.Inventory{Nodes: []wire.NodeObserved{{}}}}}
	inv, err := r.backend().Inventory(context.Background())
	if err != nil {
		t.Fatalf("Inventory: %v", err)
	}
	if len(inv.Nodes) != 1 {
		t.Fatalf("nodes = %d, want 1", len(inv.Nodes))
	}
}
