package config

import (
	"log/slog"
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
	if c.SendQueuePolicy != DropOldest {
		t.Errorf("default queue policy = %q", c.SendQueuePolicy)
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
		{"unknown policy", map[string]string{"HANGAR_CLUSTER_ID": "c", "HANGAR_HUB_URL": "wss://h/x", "HANGAR_SEND_QUEUE_POLICY": "wat"}},
		{"liveness below heartbeat", map[string]string{"HANGAR_CLUSTER_ID": "c", "HANGAR_HUB_URL": "wss://h/x", "HANGAR_HEARTBEAT_INTERVAL": "30s", "HANGAR_LIVENESS_TIMEOUT": "10s"}},
		{"bad log level", map[string]string{"HANGAR_CLUSTER_ID": "c", "HANGAR_HUB_URL": "wss://h/x", "HANGAR_LOG_LEVEL": "loud"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			for _, k := range []string{"HANGAR_CLUSTER_ID", "HANGAR_HUB_URL", "HANGAR_SEND_QUEUE_POLICY", "HANGAR_HEARTBEAT_INTERVAL", "HANGAR_LIVENESS_TIMEOUT", "HANGAR_LOG_LEVEL"} {
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
	t.Setenv("HANGAR_SEND_QUEUE_POLICY", Disconnect)
	t.Setenv("HANGAR_BACKOFF_MAX", "1m")

	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.LogLevel != slog.LevelDebug || c.SendQueueSize != 8 || c.SendQueuePolicy != Disconnect || c.BackoffMax != time.Minute {
		t.Fatalf("overrides not applied: %+v", c)
	}
}
