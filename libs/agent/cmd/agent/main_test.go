package main

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/agent/internal/agent"
)

func TestReadinessFailsOnStaleDiscovery(t *testing.T) {
	t.Parallel()

	noTunnel := errors.New("no tunnel")
	for _, tc := range []struct {
		name       string
		tunnels    error
		age        time.Duration
		staleAfter time.Duration
		want       string
	}{
		{name: "tunnels up, discovery fresh", age: time.Minute, staleAfter: 5 * time.Minute},
		{name: "tunnels down", tunnels: noTunnel, age: time.Minute, staleAfter: 5 * time.Minute, want: "no tunnel"},
		{name: "discovery stale", age: 6 * time.Minute, staleAfter: 5 * time.Minute, want: "no answer from the API server for 6m0s"},
		{name: "the check is off", age: time.Hour},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			ready := readiness(
				func() error { return tc.tunnels },
				func() time.Duration { return tc.age },
				tc.staleAfter,
			)
			err := ready()
			switch {
			case tc.want == "" && err != nil:
				t.Fatalf("not ready: %v", err)
			case tc.want != "" && (err == nil || !strings.Contains(err.Error(), tc.want)):
				t.Fatalf("readiness = %v, want an error containing %q", err, tc.want)
			}
		})
	}
}

// An agent given an explicit endpoint must start with no cluster credentials at all, which is how it
// runs off-cluster.
func TestAnExplicitEndpointNeedsNoKubernetesClient(t *testing.T) {
	t.Parallel()

	resolver, err := newResolver(&agent.Config{InferenceEndpoint: "frontend.local:8000"})
	if err != nil {
		t.Fatalf("newResolver: %v", err)
	}
	address, err := resolver.URL()
	if err != nil {
		t.Fatalf("URL: %v", err)
	}
	if address.String() != "http://frontend.local:8000" {
		t.Errorf("address = %s, want the endpoint given", address)
	}
	if age := resolver.DiscoveryAge(); age != 0 {
		t.Errorf("a configured endpoint reports discovery age %s, want 0", age)
	}
}

func TestDiscoveryWithoutClusterCredentialsSaysHowToSkipIt(t *testing.T) {
	t.Setenv("KUBERNETES_SERVICE_HOST", "")
	t.Setenv("KUBERNETES_SERVICE_PORT", "")

	_, err := newResolver(&agent.Config{WorkloadNamespace: "dynamo"})
	if err == nil || !strings.Contains(err.Error(), "HANGAR_INFERENCE_ENDPOINT") {
		t.Fatalf("newResolver off-cluster = %v, want an error naming HANGAR_INFERENCE_ENDPOINT", err)
	}
}
