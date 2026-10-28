package config

import (
	"log/slog"
	"strings"
	"testing"
	"time"
)

func TestLoadDefaults(t *testing.T) {
	t.Setenv("WINGSMITH_CLUSTER_ID", "cluster-1")
	t.Setenv("WINGSMITH_HUB_URL", "wss://hub.example/v1/stream")

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
	if c.PoolLabelKey != "wingsmith.dev/pool" || c.CacheTaintKey != "wingsmith.dev/cache" {
		t.Errorf("pool label = %q, cache taint = %q, want the wingsmith.dev defaults", c.PoolLabelKey, c.CacheTaintKey)
	}
	if c.DCGMEndpoint != "" {
		t.Errorf("dcgm endpoint = %q, want none until the cluster names one", c.DCGMEndpoint)
	}
}

func TestLoadRejectsBadInput(t *testing.T) {
	cases := []struct {
		name string
		env  map[string]string
	}{
		{"no cluster id", map[string]string{"WINGSMITH_HUB_URL": "wss://h/x"}},
		{"no hub url", map[string]string{"WINGSMITH_CLUSTER_ID": "c"}},
		{"inbound scheme", map[string]string{"WINGSMITH_CLUSTER_ID": "c", "WINGSMITH_HUB_URL": "https://h/x"}},
		{"plaintext hub", map[string]string{"WINGSMITH_CLUSTER_ID": "c", "WINGSMITH_HUB_URL": "ws://h/x"}},
		{"liveness below heartbeat", map[string]string{"WINGSMITH_CLUSTER_ID": "c", "WINGSMITH_HUB_URL": "wss://h/x", "WINGSMITH_HEARTBEAT_INTERVAL": "30s", "WINGSMITH_LIVENESS_TIMEOUT": "10s"}},
		{"bad log level", map[string]string{"WINGSMITH_CLUSTER_ID": "c", "WINGSMITH_HUB_URL": "wss://h/x", "WINGSMITH_LOG_LEVEL": "loud"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			for _, k := range []string{"WINGSMITH_CLUSTER_ID", "WINGSMITH_HUB_URL", "WINGSMITH_HUB_ALLOW_INSECURE", "WINGSMITH_HEARTBEAT_INTERVAL", "WINGSMITH_LIVENESS_TIMEOUT", "WINGSMITH_LOG_LEVEL"} {
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
	t.Setenv("WINGSMITH_CLUSTER_ID", "cluster-1")
	t.Setenv("WINGSMITH_HUB_URL", "ws://localhost:8001/v1/hub/sync")
	t.Setenv("WINGSMITH_HUB_ALLOW_INSECURE", "true")
	t.Setenv("WINGSMITH_LOG_LEVEL", "debug")
	t.Setenv("WINGSMITH_SEND_QUEUE_SIZE", "8")
	t.Setenv("WINGSMITH_BACKOFF_MAX", "1m")

	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.LogLevel != slog.LevelDebug || c.SendQueueSize != 8 || c.BackoffMax != time.Minute {
		t.Fatalf("overrides not applied: %+v", c)
	}
}

// The frontend is off until a cluster says otherwise, because with it off the operator renders
// exactly what it always did, which is what lets this binary reach every cluster before any
// cluster changes shape.
func TestTheFrontendIsOffByDefaultAndItsSwitchesAreOn(t *testing.T) {
	t.Setenv("WINGSMITH_CLUSTER_ID", "cluster-1")
	t.Setenv("WINGSMITH_HUB_URL", "wss://hub.example/v1/stream")

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
	if c.FrontendImage != "" || len(c.FrontendPullSecrets) != 0 || len(c.EnginePullSecrets) != 0 {
		t.Errorf("frontend image = %q, frontend pull secrets = %v, engine pull secrets = %v",
			c.FrontendImage, c.FrontendPullSecrets, c.EnginePullSecrets)
	}
}

func TestTheFrontendSettingsAreReadFromTheEnvironment(t *testing.T) {
	t.Setenv("WINGSMITH_CLUSTER_ID", "cluster-1")
	t.Setenv("WINGSMITH_HUB_URL", "wss://hub.example/v1/stream")
	t.Setenv("WINGSMITH_FRONTEND_ENABLED", "true")
	t.Setenv("WINGSMITH_FRONTEND_IMAGE", "ghcr.io/wingsmith-dev/vllm-runtime:1.4.0-patched.2")
	t.Setenv("WINGSMITH_FRONTEND_REPLICAS", "3")
	t.Setenv("WINGSMITH_ENGINE_PULL_SECRETS", "ghcr-engine, ghcr-mirror")
	t.Setenv("WINGSMITH_FRONTEND_PULL_SECRETS", "ghcr-wingsmith, ghcr-mirror")
	t.Setenv("WINGSMITH_FRONTEND_CPU", "500m")
	t.Setenv("WINGSMITH_FRONTEND_MEMORY", "1Gi")
	t.Setenv("WINGSMITH_FRONTEND_MEMORY_LIMIT", "4Gi")
	t.Setenv("WINGSMITH_FRONTEND_ANTHROPIC_API", "false")
	t.Setenv("WINGSMITH_FRONTEND_ANTHROPIC_STRIP_PREAMBLE", "false")
	t.Setenv("WINGSMITH_FRONTEND_GLOBAL_DYNAMO_NAMESPACE", "false")

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
		t.Errorf("frontend pull secrets = %v", c.FrontendPullSecrets)
	}
	if len(c.EnginePullSecrets) != 2 || c.EnginePullSecrets[0] != "ghcr-engine" {
		t.Errorf("engine pull secrets = %v", c.EnginePullSecrets)
	}
	if c.FrontendAnthropicAPI || c.FrontendAnthropicStripPreamble || c.FrontendGlobalDynamoNamespace {
		t.Errorf("the booleans did not turn off: %+v", c)
	}
}

// Zero replicas is a frontend that exists and serves nothing, which reads as healthy everywhere
// and is reachable by nothing. Turning the frontend off is its own setting, so say so instead of
// quietly substituting the default for what somebody wrote.
func TestZeroFrontendReplicasIsRefusedRatherThanDefaulted(t *testing.T) {
	t.Setenv("WINGSMITH_CLUSTER_ID", "cluster-1")
	t.Setenv("WINGSMITH_HUB_URL", "wss://hub.example/v1/stream")
	t.Setenv("WINGSMITH_FRONTEND_ENABLED", "true")
	t.Setenv("WINGSMITH_FRONTEND_REPLICAS", "0")

	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "WINGSMITH_FRONTEND_ENABLED") {
		t.Fatalf("error = %v, want one naming the way to render no frontend", err)
	}
}

func TestPlaintextHubNeedsTheDevFlagAndIsWarnedAbout(t *testing.T) {
	t.Setenv("WINGSMITH_CLUSTER_ID", "cluster-1")
	t.Setenv("WINGSMITH_HUB_URL", "ws://localhost:8001/v1/hub/sync")
	t.Setenv("WINGSMITH_HUB_ALLOW_INSECURE", "true")
	t.Setenv("WINGSMITH_HUB_INSECURE_SKIP_VERIFY", "true")

	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if got := c.Warnings(); len(got) != 2 {
		t.Errorf("warnings = %v, want one for ws:// and one for skip-verify", got)
	}
}

func TestTheFabricIsDeclaredWholeOrNotAtAll(t *testing.T) {
	base := map[string]string{"WINGSMITH_CLUSTER_ID": "c", "WINGSMITH_HUB_URL": "wss://h/x"}
	cases := []struct {
		name string
		env  map[string]string
		ok   bool
	}{
		{"none", nil, true},
		{"efa", map[string]string{"WINGSMITH_FABRIC": "efa", "WINGSMITH_FABRIC_DEVICES": "16"}, true},
		{"rdma with a resource", map[string]string{"WINGSMITH_FABRIC": "rdma", "WINGSMITH_FABRIC_DEVICES": "8", "WINGSMITH_FABRIC_RESOURCE": "rdma/ib"}, true},
		{"devices without a fabric", map[string]string{"WINGSMITH_FABRIC_DEVICES": "16"}, false},
		{"fabric without devices", map[string]string{"WINGSMITH_FABRIC": "efa"}, false},
		{"rdma without a resource", map[string]string{"WINGSMITH_FABRIC": "rdma", "WINGSMITH_FABRIC_DEVICES": "8"}, false},
		{"unknown fabric", map[string]string{"WINGSMITH_FABRIC": "infiniband", "WINGSMITH_FABRIC_DEVICES": "8"}, false},
		{"older efa spelling", map[string]string{"WINGSMITH_EFA_DEVICES": "16"}, true},
		{"older efa spelling at zero", map[string]string{"WINGSMITH_EFA_DEVICES": "0"}, true},
		{"both spellings", map[string]string{"WINGSMITH_EFA_DEVICES": "16", "WINGSMITH_FABRIC": "efa", "WINGSMITH_FABRIC_DEVICES": "16"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			for _, k := range []string{"WINGSMITH_FABRIC", "WINGSMITH_FABRIC_DEVICES", "WINGSMITH_FABRIC_RESOURCE", "WINGSMITH_FABRIC_PRIVILEGED", "WINGSMITH_EFA_DEVICES"} {
				t.Setenv(k, "")
			}
			for k, v := range base {
				t.Setenv(k, v)
			}
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			_, err := Load()
			if (err == nil) != tc.ok {
				t.Fatalf("error = %v, want ok=%v", err, tc.ok)
			}
		})
	}
}

// Privileged follows the fabric that needs it, and nothing else.
func TestFabricPrivilegeDefaultsOnForEFAOnly(t *testing.T) {
	t.Setenv("WINGSMITH_CLUSTER_ID", "c")
	t.Setenv("WINGSMITH_HUB_URL", "wss://h/x")
	t.Setenv("WINGSMITH_FABRIC", "efa")
	t.Setenv("WINGSMITH_FABRIC_DEVICES", "16")
	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if !c.FabricPrivileged {
		t.Error("EFA needs privileged workers to register GPU memory")
	}

	t.Setenv("WINGSMITH_FABRIC", "rdma")
	t.Setenv("WINGSMITH_FABRIC_RESOURCE", "rdma/ib")
	if c, err = Load(); err != nil {
		t.Fatal(err)
	}
	if c.FabricPrivileged {
		t.Error("a non-EFA fabric runs privileged without asking")
	}
}

// Charts before 0.3.0 wrote WINGSMITH_EFA_DEVICES. Dropping it silently would turn the fabric off on
// every such install and move KV over TCP with nothing saying so.
func TestTheOlderEFASpellingStillDeclaresTheFabric(t *testing.T) {
	t.Setenv("WINGSMITH_CLUSTER_ID", "c")
	t.Setenv("WINGSMITH_HUB_URL", "wss://h/x")
	t.Setenv("WINGSMITH_EFA_DEVICES", "16")
	t.Setenv("WINGSMITH_FABRIC_RESOURCE", "vpc.amazonaws.com/efa")
	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.Fabric != "efa" || c.FabricDevices != 16 || !c.FabricPrivileged {
		t.Errorf("fabric = %q x%d privileged=%v, want efa x16 privileged", c.Fabric, c.FabricDevices, c.FabricPrivileged)
	}
	warned := false
	for _, w := range c.Warnings() {
		warned = warned || strings.Contains(w, "WINGSMITH_EFA_DEVICES")
	}
	if !warned {
		t.Errorf("warnings = %v, want one naming WINGSMITH_EFA_DEVICES", c.Warnings())
	}
}
