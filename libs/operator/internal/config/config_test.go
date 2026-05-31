package config

import (
	"log/slog"
	"strings"
	"testing"
	"time"
)

func TestLoadDefaults(t *testing.T) {
	t.Setenv("HANGAR_CLUSTER_ID", "cluster-1")
	t.Setenv("HANGAR_HUB_URL", "wss://hub.example/v1/stream")

	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.LogLevel != slog.LevelInfo {
		t.Errorf("default log level = %v", c.LogLevel)
	}
	if c.LivenessTimeout <= c.HeartbeatInterval {
		t.Errorf("liveness timeout must exceed the heartbeat interval")
	}
}

func TestLoadRejectsBadInput(t *testing.T) {
	cases := []struct {
		name string
		env  map[string]string
	}{
		{"no cluster id", map[string]string{"HANGAR_HUB_URL": "wss://h/x"}},
		{"no hub url", map[string]string{"HANGAR_CLUSTER_ID": "c"}},
		{"inbound scheme", map[string]string{"HANGAR_CLUSTER_ID": "c", "HANGAR_HUB_URL": "https://h/x"}},
		{"liveness below heartbeat", map[string]string{"HANGAR_CLUSTER_ID": "c", "HANGAR_HUB_URL": "wss://h/x", "HANGAR_HEARTBEAT_INTERVAL": "30s", "HANGAR_LIVENESS_TIMEOUT": "10s"}},
		{"bad log level", map[string]string{"HANGAR_CLUSTER_ID": "c", "HANGAR_HUB_URL": "wss://h/x", "HANGAR_LOG_LEVEL": "loud"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			for _, k := range []string{"HANGAR_CLUSTER_ID", "HANGAR_HUB_URL", "HANGAR_HEARTBEAT_INTERVAL", "HANGAR_LIVENESS_TIMEOUT", "HANGAR_LOG_LEVEL"} {
				t.Setenv(k, "")
			}
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			if _, err := Load(); err == nil {
				t.Fatal("expected an error")
			}
		})
	}
}

func TestLoadOverrides(t *testing.T) {
	t.Setenv("HANGAR_CLUSTER_ID", "cluster-1")
	t.Setenv("HANGAR_HUB_URL", "ws://localhost:8080/stream")
	t.Setenv("HANGAR_LOG_LEVEL", "debug")
	t.Setenv("HANGAR_SEND_QUEUE_SIZE", "8")
	t.Setenv("HANGAR_BACKOFF_MAX", "1m")

	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.LogLevel != slog.LevelDebug || c.SendQueueSize != 8 || c.BackoffMax != time.Minute {
		t.Fatalf("overrides not applied: %+v", c)
	}
}

// The frontend is off until a cluster says otherwise, because with it off the operator renders
// exactly what it always did — which is what lets this binary reach every cluster before any
// cluster changes shape.
func TestTheFrontendIsOffByDefaultAndItsSwitchesAreOn(t *testing.T) {
	t.Setenv("HANGAR_CLUSTER_ID", "cluster-1")
	t.Setenv("HANGAR_HUB_URL", "wss://hub.example/v1/stream")

	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.FrontendEnabled {
		t.Error("the frontend renders on a cluster that never asked for it")
	}
	// Two, so a rollout does not take a model's only frontend with it.
	if c.FrontendReplicas != 2 {
		t.Errorf("frontend replicas = %d", c.FrontendReplicas)
	}
	// Both default on, matching the chart: a cluster without the Anthropic surface cannot be
	// used by Anthropic-protocol clients at all.
	if !c.FrontendAnthropicAPI || !c.FrontendAnthropicStripPreamble {
		t.Errorf("anthropic switches = %v, %v", c.FrontendAnthropicAPI, c.FrontendAnthropicStripPreamble)
	}
	// True is what every cluster runs today, and the flag exists so the answer can change
	// without the renderer changing.
	if !c.FrontendGlobalDynamoNamespace {
		t.Error("the frontend would be narrowed to its own namespace by default, which nothing has shown to work")
	}
	if c.FrontendImage != "" || len(c.FrontendPullSecrets) != 0 {
		t.Errorf("frontend image = %q, pull secrets = %v", c.FrontendImage, c.FrontendPullSecrets)
	}
}

func TestTheFrontendSettingsAreReadFromTheEnvironment(t *testing.T) {
	t.Setenv("HANGAR_CLUSTER_ID", "cluster-1")
	t.Setenv("HANGAR_HUB_URL", "wss://hub.example/v1/stream")
	t.Setenv("HANGAR_FRONTEND_ENABLED", "true")
	t.Setenv("HANGAR_FRONTEND_IMAGE", "ghcr.io/hangar-dev/vllm-runtime:1.4.0-patched.1-hgr2")
	t.Setenv("HANGAR_FRONTEND_REPLICAS", "3")
	t.Setenv("HANGAR_FRONTEND_PULL_SECRETS", "ghcr-hangar, ghcr-mirror")
	t.Setenv("HANGAR_FRONTEND_CPU", "500m")
	t.Setenv("HANGAR_FRONTEND_MEMORY", "1Gi")
	t.Setenv("HANGAR_FRONTEND_MEMORY_LIMIT", "4Gi")
	t.Setenv("HANGAR_FRONTEND_ANTHROPIC_API", "false")
	t.Setenv("HANGAR_FRONTEND_ANTHROPIC_STRIP_PREAMBLE", "false")
	t.Setenv("HANGAR_FRONTEND_GLOBAL_DYNAMO_NAMESPACE", "false")

	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if !c.FrontendEnabled || c.FrontendReplicas != 3 {
		t.Errorf("enabled = %v, replicas = %d", c.FrontendEnabled, c.FrontendReplicas)
	}
	if c.FrontendImage == "" || c.FrontendCPU != "500m" || c.FrontendMemory != "1Gi" || c.FrontendMemoryLimit != "4Gi" {
		t.Errorf("frontend image and resources not applied: %+v", c)
	}
	// A pull secret list written with spaces after the commas is the ordinary way somebody
	// writes one into a ConfigMap.
	if len(c.FrontendPullSecrets) != 2 || c.FrontendPullSecrets[1] != "ghcr-mirror" {
		t.Errorf("pull secrets = %v", c.FrontendPullSecrets)
	}
	if c.FrontendAnthropicAPI || c.FrontendAnthropicStripPreamble || c.FrontendGlobalDynamoNamespace {
		t.Errorf("the booleans did not turn off: %+v", c)
	}
}

// Zero replicas is a frontend that exists and serves nothing, which reads as healthy everywhere
// and is reachable by nothing. Turning the frontend off is its own setting, so say so rather than
// quietly substitute the default for what somebody wrote.
func TestZeroFrontendReplicasIsRefusedRatherThanDefaulted(t *testing.T) {
	t.Setenv("HANGAR_CLUSTER_ID", "cluster-1")
	t.Setenv("HANGAR_HUB_URL", "wss://hub.example/v1/stream")
	t.Setenv("HANGAR_FRONTEND_ENABLED", "true")
	t.Setenv("HANGAR_FRONTEND_REPLICAS", "0")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "HANGAR_FRONTEND_ENABLED") {
		t.Fatalf("error = %v, want one naming the way to render no frontend", err)
	}
}
