package observe

import (
	"context"
	"testing"
)

func TestInventoryProjectsWithoutSending(t *testing.T) {
	o := newTestObserver(t, NewFake(sampleSnapshot()))
	inv, err := o.Inventory(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(inv.Nodes) == 0 || len(inv.Devices) == 0 || inv.Cluster.ClusterID != testCluster {
		t.Fatalf("inventory missing supply: %+v", inv)
	}
	if n := len(o.recorder(t).frames); n != 0 {
		t.Fatalf("inventory sent %d frames", n)
	}
	if _, seen := o.Since(sampleSnapshot().At); seen {
		t.Fatal("inventory advanced the loop's state")
	}
}
