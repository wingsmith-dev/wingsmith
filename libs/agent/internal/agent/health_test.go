package agent

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestProbesLivenessIsUnconditional(t *testing.T) {
	handler := ProbeHandler(ProbeConfig{Ready: func() error { return errors.New("no tunnel to any gateway") }})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Errorf("healthz = %d, want 200 even with no tunnel", rec.Code)
	}
}

func TestProbesReadinessCarriesCoverage(t *testing.T) {
	coverage := map[string]int{"gw-0.gw:9443": 0, "gw-1.gw:9443": 2}
	handler := ProbeHandler(ProbeConfig{
		Ready:    func() error { return nil },
		Coverage: func() map[string]int { return coverage },
	})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("readyz = %d, want 200", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{"gw-0.gw:9443: 0", "gw-1.gw:9443: 2"} {
		if !strings.Contains(body, want) {
			t.Errorf("body %q does not carry %q", body, want)
		}
	}
}

func TestProbesReadinessRefusesWithoutATunnel(t *testing.T) {
	handler := ProbeHandler(ProbeConfig{Ready: func() error { return errors.New("no tunnel to any gateway") }})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("readyz = %d, want 503", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "no tunnel") {
		t.Errorf("body %q does not say why", rec.Body.String())
	}
}

func TestProbesServeMetrics(t *testing.T) {
	metrics := NewMetrics()
	metrics.track("gw-0.gw:9443")
	handler := ProbeHandler(ProbeConfig{Metrics: metrics})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("metrics = %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "hangar_agent_target_up") {
		t.Error("the probe listener does not serve the agent's metrics")
	}
}
