package metrics

import (
	"testing"

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
	if got, ok := labelled(t, recorder, "hangar_gateway_tunnels", map[string]string{"cluster_id": "clu_ap"}); !ok || got != 2 {
		t.Fatalf("tunnels = %v (%v)", got, ok)
	}

	recorder.TunnelClosed("clu_ap", 0)
	if got, _ := labelled(t, recorder, "hangar_gateway_tunnels", map[string]string{"cluster_id": "clu_ap"}); got != 0 {
		t.Fatalf("tunnels = %v after the last one closed", got)
	}

	recorder.TunnelDialRejected("unauthorized")
	if got, ok := labelled(t, recorder, "hangar_gateway_tunnel_dials_rejected_total", map[string]string{"reason": "unauthorized"}); !ok || got != 1 {
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

	if got, _ := labelled(t, recorder, "hangar_gateway_tunnel_coverage", map[string]string{"cluster_id": "clu_ap", "cluster": "ap"}); got != 1 {
		t.Fatalf("ap coverage = %v", got)
	}
	if got, _ := labelled(t, recorder, "hangar_gateway_tunnel_coverage", map[string]string{"cluster_id": "clu_us", "cluster": "us"}); got != 0 {
		t.Fatalf("us coverage = %v", got)
	}

	recorder.TunnelCoverage([]TunnelSample{{ClusterID: "clu_ap", Cluster: "ap", Reachable: true}})
	if _, ok := labelled(t, recorder, "hangar_gateway_tunnel_coverage", map[string]string{"cluster_id": "clu_us", "cluster": "us"}); ok {
		t.Fatal("a cluster that left the table is still reporting coverage")
	}
}
