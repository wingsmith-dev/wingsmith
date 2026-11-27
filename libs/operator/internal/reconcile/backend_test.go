package reconcile

import (
	"context"
	"testing"
	"time"
)

func TestRolloutStatusKeepsVocabulary(t *testing.T) {
	at := time.Date(2026, 11, 27, 0, 0, 0, 0, time.UTC)
	cases := []struct {
		in         rollout
		phase      string
		reason     string
		converging bool
	}{
		{rollout{desired: 2, ready: 2}, phaseRunning, "Serving", false},
		{rollout{desired: 2, ready: 1}, phaseDegraded, "Progressing", true},
		{rollout{desired: 2, ready: 2, frontendUnready: true}, phaseDegraded, "Progressing", true},
		{rollout{desired: 1}, phasePlaced, "Applied", true},
	}
	for _, c := range cases {
		got := c.in.status(at)
		if got.Phase != c.phase || got.Reason != c.reason || got.Converging != c.converging ||
			got.Message != c.in.message() || got.ObservedAt != at {
			t.Errorf("%+v: got %+v", c.in, got)
		}
	}
}

func TestKubeBackendInventoryNeedsSource(t *testing.T) {
	if _, err := (&KubeBackend{}).Inventory(context.Background()); err == nil {
		t.Fatal("inventory without a source succeeded")
	}
}
