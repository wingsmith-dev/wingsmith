package metrics

import (
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/packages/tunnel"

	dto "github.com/prometheus/client_model/go"
)

func family(t *testing.T, recorder *Recorder, name string) []*dto.Metric {
	t.Helper()
	all, err := recorder.Gatherer().Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, metric := range all {
		if metric.GetName() == name {
			return metric.GetMetric()
		}
	}
	return nil
}

func labelled(t *testing.T, recorder *Recorder, name string, want map[string]string) (float64, bool) {
	t.Helper()
	for _, metric := range family(t, recorder, name) {
		matches := true
		for _, pair := range metric.GetLabel() {
			if value, ok := want[pair.GetName()]; ok && value != pair.GetValue() {
				matches = false
			}
		}
		if matches && len(metric.GetLabel()) == len(want) {
			if metric.Gauge != nil {
				return metric.Gauge.GetValue(), true
			}
			return metric.Counter.GetValue(), true
		}
	}
	return 0, false
}

func TestTunnelCountsFollowThePool(t *testing.T) {
	recorder := New()

	recorder.TunnelOpened("clu_ap", 1)
	recorder.TunnelOpened("clu_ap", 2)
	if got, ok := labelled(t, recorder, "wingsmith_gateway_tunnels", map[string]string{"cluster_id": "clu_ap"}); !ok || got != 2 {
		t.Fatalf("tunnels = %v (%v)", got, ok)
	}

	recorder.TunnelClosed("clu_ap", 0)
	if got, _ := labelled(t, recorder, "wingsmith_gateway_tunnels", map[string]string{"cluster_id": "clu_ap"}); got != 0 {
		t.Fatalf("tunnels = %v after the last one closed", got)
	}

	recorder.TunnelDialRejected("unauthorized")
	if got, ok := labelled(t, recorder, "wingsmith_gateway_tunnel_dials_rejected_total", map[string]string{"reason": "unauthorized"}); !ok || got != 1 {
		t.Fatalf("rejected = %v (%v)", got, ok)
	}
}

// TestCoverageDropsClustersThatLeaveTheTable. A stuck zero on a cluster nobody
// routes to any more is an alert that cannot be cleared.
func TestCoverageDropsClustersThatLeaveTheTable(t *testing.T) {
	recorder := New()
	recorder.TunnelCoverage([]TunnelSample{
		{ClusterID: "clu_ap", Cluster: "ap", Reachable: true},
		{ClusterID: "clu_us", Cluster: "us"},
	})

	if got, _ := labelled(t, recorder, "wingsmith_gateway_tunnel_coverage", map[string]string{"cluster_id": "clu_ap", "cluster": "ap"}); got != 1 {
		t.Fatalf("ap coverage = %v", got)
	}
	if got, _ := labelled(t, recorder, "wingsmith_gateway_tunnel_coverage", map[string]string{"cluster_id": "clu_us", "cluster": "us"}); got != 0 {
		t.Fatalf("us coverage = %v", got)
	}

	recorder.TunnelCoverage([]TunnelSample{{ClusterID: "clu_ap", Cluster: "ap", Reachable: true}})
	if _, ok := labelled(t, recorder, "wingsmith_gateway_tunnel_coverage", map[string]string{"cluster_id": "clu_us", "cluster": "us"}); ok {
		t.Fatal("a cluster that left the table is still reporting coverage")
	}
}

// The listener and the pool find these by type assertion, so a renamed method
// would silently stop counting.
var (
	_ tunnel.DuplicateObserver  = (*Recorder)(nil)
	_ tunnel.RevocationObserver = (*Recorder)(nil)
	_ tunnel.RetryObserver      = (*Recorder)(nil)
)

// TestDuplicatesRevocationsAndRetriesAreTheirOwnSeries. A duplicate dial is how
// an agent finds every replica and a revocation is the plane working; folding
// either into the refused count would make both look like failures.
func TestDuplicatesRevocationsAndRetriesAreTheirOwnSeries(t *testing.T) {
	recorder := New()

	recorder.TunnelDialDuplicate()
	recorder.TunnelDialDuplicate()
	if got, ok := labelled(t, recorder, "wingsmith_gateway_tunnel_dials_duplicate_total", map[string]string{}); !ok || got != 2 {
		t.Fatalf("duplicates = %v (%v)", got, ok)
	}
	if _, ok := labelled(t, recorder, "wingsmith_gateway_tunnel_dials_rejected_total", map[string]string{"reason": "duplicate"}); ok {
		t.Fatal("a duplicate was counted as a refused dial")
	}

	recorder.TunnelRevoked("clu_ap")
	if got, ok := labelled(t, recorder, "wingsmith_gateway_tunnels_revoked_total", map[string]string{"cluster_id": "clu_ap"}); !ok || got != 1 {
		t.Fatalf("revoked = %v (%v)", got, ok)
	}

	recorder.TunnelRetry("clu_ap", tunnel.RetryWritten)
	recorder.TunnelRetry("clu_ap", tunnel.RetryResent)
	recorder.TunnelRetry("clu_ap", tunnel.RetryResent)
	if got, ok := labelled(t, recorder, "wingsmith_gateway_tunnel_retries_total", map[string]string{"cluster_id": "clu_ap", "outcome": "written"}); !ok || got != 1 {
		t.Fatalf("written = %v (%v)", got, ok)
	}
	if got, ok := labelled(t, recorder, "wingsmith_gateway_tunnel_retries_total", map[string]string{"cluster_id": "clu_ap", "outcome": "resent"}); !ok || got != 2 {
		t.Fatalf("resent = %v (%v)", got, ok)
	}
}

func TestTunnelStreamUseIsReadAtScrape(t *testing.T) {
	recorder := New()
	samples := []StreamSample{{ClusterID: "clu_ap", Active: 3, Capacity: 10}}
	recorder.WatchTunnelStreams(func() []StreamSample { return samples })

	labels := map[string]string{"cluster_id": "clu_ap"}
	if got, ok := labelled(t, recorder, "wingsmith_gateway_tunnel_streams_active", labels); !ok || got != 3 {
		t.Fatalf("streams active = %v (%v), want 3", got, ok)
	}
	if got, ok := labelled(t, recorder, "wingsmith_gateway_tunnel_streams_capacity", labels); !ok || got != 10 {
		t.Fatalf("streams capacity = %v (%v), want 10", got, ok)
	}

	samples = nil
	if got := family(t, recorder, "wingsmith_gateway_tunnel_streams_active"); len(got) != 0 {
		t.Fatalf("a cluster with no live tunnel still has %d stream series", len(got))
	}
}
