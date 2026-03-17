// Package config loads operator configuration from the environment.
package config

import (
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config is the fully resolved operator configuration.
type Config struct {
	// ClusterID is the plane-assigned identity of the cluster this operator instance runs in.
	ClusterID string

	// HubURL is the outbound WebSocket endpoint on the control plane. Only ws:// and wss:// are
	// accepted; nothing ever connects inward.
	HubURL string

	// EnrolmentToken is a static bearer token presented on dial. Auth is stubbed: the real
	// credential and rotation model is an open question in docs/plans/01 §11.5.
	EnrolmentToken string

	// TLSInsecureSkipVerify disables hub certificate verification. Local development only.
	TLSInsecureSkipVerify bool

	// TLSCAFile is an optional PEM bundle used to verify the hub certificate.
	TLSCAFile string

	// LogLevel is one of debug, info, warn, error.
	LogLevel slog.Level

	// LogFormat is json or text.
	LogFormat string

	// HealthAddr is the listen address for the health and readiness endpoints.
	HealthAddr string

	// MetricsAddr is the listen address for controller-runtime metrics, or "0" to disable.
	MetricsAddr string

	// SendQueueSize bounds the outbound frame queue. The overflow policy is not configurable:
	// PROTOCOL §6 fixes it at replace-by-subject with events dropped from the tail.
	SendQueueSize int

	// HeartbeatInterval is how often the operator sends an application-level ping.
	HeartbeatInterval time.Duration

	// LivenessTimeout is how long the operator tolerates silence from the hub before declaring
	// the connection dead and redialling. It must exceed HeartbeatInterval.
	LivenessTimeout time.Duration

	// ObserveInterval is how often the operator re-reads its own cluster's supply and sends
	// what changed. It is a resolution knob, not a correctness one: the observed stream is
	// full-object and resync fills any hole, so a slower interval only ages the hub's view.
	ObserveInterval time.Duration

	// BackoffMin and BackoffMax bound the reconnect backoff.
	BackoffMin time.Duration
	BackoffMax time.Duration

	// LeaderElection enables controller-runtime leader election.
	LeaderElection bool

	// Namespace is where the operator stores CRDs and its leader-election lease.
	Namespace string
}

// Load reads configuration from the environment, applying defaults and validating the result.
func Load() (*Config, error) {
	c := &Config{
		ClusterID:             env("HANGAR_CLUSTER_ID", ""),
		HubURL:                env("HANGAR_HUB_URL", ""),
		EnrolmentToken:        env("HANGAR_ENROLMENT_TOKEN", ""),
		TLSCAFile:             env("HANGAR_HUB_CA_FILE", ""),
		TLSInsecureSkipVerify: envBool("HANGAR_HUB_INSECURE_SKIP_VERIFY", false),
		LogFormat:             env("HANGAR_LOG_FORMAT", "json"),
		HealthAddr:            env("HANGAR_HEALTH_ADDR", ":8081"),
		MetricsAddr:           env("HANGAR_METRICS_ADDR", "0"),
		SendQueueSize:         envInt("HANGAR_SEND_QUEUE_SIZE", 1024),
		HeartbeatInterval:     envDuration("HANGAR_HEARTBEAT_INTERVAL", 10*time.Second),
		LivenessTimeout:       envDuration("HANGAR_LIVENESS_TIMEOUT", 30*time.Second),
		ObserveInterval:       envDuration("HANGAR_OBSERVE_INTERVAL", 30*time.Second),
		BackoffMin:            envDuration("HANGAR_BACKOFF_MIN", time.Second),
		BackoffMax:            envDuration("HANGAR_BACKOFF_MAX", 30*time.Second),
		LeaderElection:        envBool("HANGAR_LEADER_ELECTION", false),
		Namespace:             env("HANGAR_NAMESPACE", "hangar-system"),
	}

	lvl, err := parseLevel(env("HANGAR_LOG_LEVEL", "info"))
	if err != nil {
		return nil, err
	}
	c.LogLevel = lvl

	if err := c.validate(); err != nil {
		return nil, err
	}
	return c, nil
}

func (c *Config) validate() error {
	if c.ClusterID == "" {
		return fmt.Errorf("HANGAR_CLUSTER_ID is required")
	}
	if c.HubURL == "" {
		return fmt.Errorf("HANGAR_HUB_URL is required")
	}
	u, err := url.Parse(c.HubURL)
	if err != nil {
		return fmt.Errorf("HANGAR_HUB_URL is not a URL: %w", err)
	}
	if u.Scheme != "ws" && u.Scheme != "wss" {
		return fmt.Errorf("HANGAR_HUB_URL scheme must be ws or wss, got %q", u.Scheme)
	}
	if c.SendQueueSize < 1 {
		return fmt.Errorf("HANGAR_SEND_QUEUE_SIZE must be positive")
	}
	if c.LivenessTimeout <= c.HeartbeatInterval {
		return fmt.Errorf("HANGAR_LIVENESS_TIMEOUT (%s) must exceed HANGAR_HEARTBEAT_INTERVAL (%s)", c.LivenessTimeout, c.HeartbeatInterval)
	}
	if c.ObserveInterval <= 0 {
		return fmt.Errorf("HANGAR_OBSERVE_INTERVAL must be positive")
	}
	if c.BackoffMax < c.BackoffMin {
		return fmt.Errorf("HANGAR_BACKOFF_MAX must be >= HANGAR_BACKOFF_MIN")
	}
	if c.LogFormat != "json" && c.LogFormat != "text" {
		return fmt.Errorf("HANGAR_LOG_FORMAT must be json or text")
	}
	return nil
}

func parseLevel(s string) (slog.Level, error) {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "debug":
		return slog.LevelDebug, nil
	case "info", "":
		return slog.LevelInfo, nil
	case "warn", "warning":
		return slog.LevelWarn, nil
	case "error":
		return slog.LevelError, nil
	default:
		return 0, fmt.Errorf("HANGAR_LOG_LEVEL %q is not one of debug, info, warn, error", s)
	}
}

func env(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return def
}

func envBool(key string, def bool) bool {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		return def
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		return def
	}
	return b
}

func envInt(key string, def int) int {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func envDuration(key string, def time.Duration) time.Duration {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return def
	}
	return d
}
