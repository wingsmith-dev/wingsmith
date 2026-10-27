package observe

import "testing"

// The two backends Dynamo can render onto, under the group names they actually serve, plus the
// versioned and vendor-prefixed spellings a rename would plausibly produce.
func TestMultinodeCapableRecognisesABackend(t *testing.T) {
	for _, group := range []string{
		"grove.io",
		"scheduler.grove.io",
		"grove.nvidia.com",
		"leaderworkerset.x-k8s.io",
		"LeaderWorkerSet.x-k8s.io",
	} {
		if !multinodeCapable([]string{"apps", "batch", group}) {
			t.Errorf("group %q should read as a leader-worker backend", group)
		}
	}
}

// A cluster with no backend must read false, not nearly-true. The failure this guards is
// silent: claiming a capability the cluster does not have renders a pod that hangs in collective
// initialisation instead of parking with a reason.
func TestMultinodeCapableIsFalseWithoutABackend(t *testing.T) {
	groups := []string{
		"",
		"apps",
		"batch",
		"nvidia.com",
		"kai.scheduler",
		"dynamo.nvidia.com",
	}
	if multinodeCapable(groups) {
		t.Fatalf("no group in %v is a leader-worker backend", groups)
	}
}

// Discovery that returns nothing is not evidence of a backend.
func TestMultinodeCapableIsFalseOnAnEmptyReading(t *testing.T) {
	if multinodeCapable(nil) {
		t.Fatal("an empty group listing must not read as capable")
	}
}

// A nil source is the operator that could not build a discovery client. It reports no groups
// instead of failing, and the caller reads that as no backend.
func TestDiscoveryGroupsIsEmptyWhenUnset(t *testing.T) {
	var source *DiscoveryGroups
	groups, err := source.Groups()
	if err != nil {
		t.Fatalf("a nil source should not error: %v", err)
	}
	if len(groups) != 0 {
		t.Fatalf("groups = %v, want none", groups)
	}
}

// The reconcile loop reads this holder on its own clock, so an unpublished one must read false
// instead of panicking or reporting a capability nothing observed.
func TestCapabilityDefaultsToIncapable(t *testing.T) {
	var unset *Capability
	if unset.Multinode() {
		t.Error("a nil holder must read false")
	}
	fresh := &Capability{}
	if fresh.Multinode() {
		t.Error("a holder nothing has published to must read false")
	}
	fresh.Publish(true)
	if !fresh.Multinode() {
		t.Error("a published capability must be readable")
	}
	fresh.Publish(false)
	if fresh.Multinode() {
		t.Error("a backend that went away must stop being reported")
	}
}
