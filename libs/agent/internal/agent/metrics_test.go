package agent

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	dto "github.com/prometheus/client_model/go"
)

// The replica label, for readability. It is the pod name of a gateway replica in production.
const testGateway = "hangar-gateway-0"

func TestMetricsTrackReportsAReachedReplicaWithNoTunnelAsZero(t *testing.T) {
	m := NewMetrics()
	m.track(testGateway)

	if got := gaugeValue(t, m, "hangar_agent_tunnels", map[string]string{"gateway": testGateway}); got != 0 {
		t.Errorf("tunnels = %v, want an explicit 0", got)
	}
	if got := gaugeValue(t, m, "hangar_agent_gateway_up", map[string]string{"gateway": testGateway}); got != 0 {
		t.Errorf("gateway_up = %v, want 0", got)
	}
}

func TestMetricsFollowTunnelState(t *testing.T) {
	m := NewMetrics()
	m.track(testGateway)
	m.dialled()
	m.tunnelOpened(testGateway, 1)

	if got := gaugeValue(t, m, "hangar_agent_gateway_up", map[string]string{"gateway": testGateway}); got != 1 {
		t.Errorf("gateway_up after opening = %v, want 1", got)
	}

	m.tunnelClosed(testGateway, 0, 12)
	if got := gaugeValue(t, m, "hangar_agent_tunnels", map[string]string{"gateway": testGateway}); got != 0 {
		t.Errorf("tunnels after closing = %v, want 0", got)
	}
	if got := gaugeValue(t, m, "hangar_agent_gateway_up", map[string]string{"gateway": testGateway}); got != 0 {
		t.Errorf("gateway_up after closing = %v, want 0", got)
	}
}

// A replica the gateway has scaled past has to leave no series behind, or a gauge stuck at zero
// alerts about a missing tunnel to something that does not exist.
func TestMetricsForgetARetiredReplica(t *testing.T) {
	m := NewMetrics()
	m.track(testGateway)
	m.tunnelOpened(testGateway, 1)
	m.forget(testGateway)

	for _, name := range []string{"hangar_agent_tunnels", "hangar_agent_gateway_up"} {
		if _, err := seriesFor(m, name, map[string]string{"gateway": testGateway}); err == nil {
			t.Errorf("%s still carries the retired replica", name)
		}
	}
}

// The dial counters have to read zero rather than be absent from the very first scrape: an absent
// counter and a counter that has never moved are the same query result, and one of them is an agent
// that is not running.
func TestDialCountersExistBeforeAnyDial(t *testing.T) {
	m := NewMetrics()
	if got := counterValue(t, m, "hangar_agent_dial_attempts_total", nil); got != 0 {
		t.Errorf("dial attempts = %v, want an explicit 0", got)
	}
	for _, reason := range []string{reasonDial, reasonUnauthorized, reasonHandshake} {
		if got := counterValue(t, m, "hangar_agent_dial_failures_total",
			map[string]string{"reason": reason}); got != 0 {
			t.Errorf("dial failures for %s = %v, want an explicit 0", reason, got)
		}
	}
}

func TestMetricsHandlerExposes(t *testing.T) {
	m := NewMetrics()
	m.track(testGateway)

	rec := httptest.NewRecorder()
	m.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "hangar_agent_tunnels") {
		t.Errorf("exposition does not carry the tunnel gauge:\n%s", rec.Body.String())
	}
}

// gaugeValue and counterValue read one series off the registry, which is the only way to see what a
// scrape would see.
func gaugeValue(t *testing.T, m *Metrics, name string, labels map[string]string) float64 {
	t.Helper()
	return seriesValue(t, m, name, labels, true)
}

func counterValue(t *testing.T, m *Metrics, name string, labels map[string]string) float64 {
	t.Helper()
	return seriesValue(t, m, name, labels, false)
}

func seriesValue(t *testing.T, m *Metrics, name string, labels map[string]string, gauge bool) float64 {
	t.Helper()
	metric, err := seriesFor(m, name, labels)
	if err != nil {
		t.Fatal(err)
	}
	if gauge {
		return metric.GetGauge().GetValue()
	}
	return metric.GetCounter().GetValue()
}

// seriesFor finds one series and reports its absence rather than failing, so a test can assert that
// something is *not* exported.
func seriesFor(m *Metrics, name string, labels map[string]string) (*dto.Metric, error) {
	families, err := m.Registry().Gather()
	if err != nil {
		return nil, fmt.Errorf("gather: %w", err)
	}
	for _, family := range families {
		if family.GetName() != name {
			continue
		}
		for _, metric := range family.GetMetric() {
			if matches(metric.GetLabel(), labels) {
				return metric, nil
			}
		}
	}
	return nil, fmt.Errorf("no series %s%v in the registry", name, labels)
}

func matches(pairs []*dto.LabelPair, want map[string]string) bool {
	for key, value := range want {
		found := false
		for _, pair := range pairs {
			if pair.GetName() == key && pair.GetValue() == value {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}
