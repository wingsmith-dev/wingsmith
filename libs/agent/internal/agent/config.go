package agent

import (
	"fmt"
	"log/slog"
	"math"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config is the fully resolved agent configuration.
type Config struct {
	// ClusterID is the plane-assigned identity of the cluster this agent runs in. It is
	// presented on every dial so the gateway knows which cluster the tunnel reaches; the
	// gateway still authenticates the token, so this is a claim and not a credential.
	ClusterID string

	// Gateway is the one address to dial, as host:port.
	//
	// One address and not a list of replicas, even though a tunnel to every replica is what
	// coverage requires. How many replicas there are is the gateway's own business and it says so
	// in the handshake — so scaling the gateway is a change in the plane's repository and not an
	// edit to the values of every cluster in the fleet.
	Gateway string

	// EnrolmentToken is the per-cluster bearer token presented on dial. It is the same
	// credential the operator uses, delivered by the chart into the same Secret — TLS carries
	// confidentiality, this carries identity, and there is no CA and no mTLS.
	EnrolmentToken string

	// ConnsPerGateway is how many tunnels to hold to each gateway replica.
	//
	// More than one on purpose, for two reasons. HTTP/2 retries a request the far end says it
	// never processed, and the gateway can only retry it down a *different* tunnel to the same
	// cluster — so with one tunnel per replica, an agent rollout turns those retries into 502s on
	// the replicas that hold only that one. And a single multiplexed connection carrying every
	// request to a cluster makes head-of-line blocking in one stream everyone else's problem.
	ConnsPerGateway int

	// MaxConcurrentStreams is how many requests one tunnel carries at once, advertised to the
	// gateway in SETTINGS. Zero takes the tunnel package's default. Past it the gateway answers
	// "no capacity" rather than queueing, so this and ConnsPerGateway together decide when
	// overload is an error instead of latency.
	MaxConcurrentStreams int

	// TLSEnabled dials TCP+TLS. Turning it off is for a local kind cluster where the gateway
	// has no certificate; nothing that leaves a cluster network should ever run without it.
	TLSEnabled bool

	// TLSServerName overrides the name verified in the gateway's certificate. Rarely needed now
	// that there is one address: the name dialled is the name the certificate is issued for.
	TLSServerName string

	// TLSCAFile is an optional PEM bundle used to verify the gateway's certificate.
	TLSCAFile string

	// TLSInsecureSkipVerify disables certificate verification. Local development only.
	TLSInsecureSkipVerify bool

	// DialTimeout bounds the TCP connect and the TLS handshake.
	DialTimeout time.Duration

	// HandshakeTimeout bounds the tunnel preface — the request that presents the token and the
	// response that accepts it. It is separate from DialTimeout because a gateway replica that
	// accepts connections and then never answers is a different failure from one that cannot be
	// reached, and only a deadline on this one turns it into a redial rather than a wedge.
	HandshakeTimeout time.Duration

	// BackoffMin and BackoffMax bound the per-target reconnect backoff.
	BackoffMin time.Duration
	BackoffMax time.Duration

	// DrainTimeout is how long shutdown waits for in-flight streams after GOAWAY. Every one of
	// them is somebody's half-written completion, so it is generous.
	DrainTimeout time.Duration

	// HealthAddr is the listen address for the health, readiness and scrape endpoints.
	HealthAddr string

	// InferenceEndpoint is this cluster's OpenAI-compatible frontend, as a URL or host:port.
	// Empty means find it by asking the cluster for the Service that selects Dynamo's frontend
	// component, which is what the operator's identically named setting does.
	InferenceEndpoint string

	// WorkloadNamespace is where the graphs run, and so where frontend discovery looks. Ignored
	// when InferenceEndpoint is set.
	WorkloadNamespace string

	// DiscoveryInterval is how often frontend discovery re-asks the cluster. Zero takes the
	// frontend package's default.
	DiscoveryInterval time.Duration

	// LogLevel is one of debug, info, warn, error.
	LogLevel slog.Level

	// LogFormat is json or text.
	LogFormat string
}

// Load reads configuration from the environment, applying defaults and validating the result.
func Load() (*Config, error) {
	c := &Config{
		ClusterID:             env("HANGAR_CLUSTER_ID", ""),
		Gateway:               env("HANGAR_TUNNEL_GATEWAY", ""),
		EnrolmentToken:        env("HANGAR_ENROLMENT_TOKEN", ""),
		ConnsPerGateway:       envInt("HANGAR_TUNNEL_CONNS_PER_GATEWAY", 2),
		MaxConcurrentStreams:  envInt("HANGAR_TUNNEL_MAX_CONCURRENT_STREAMS", 0),
		TLSEnabled:            envBool("HANGAR_TUNNEL_TLS", true),
		TLSServerName:         env("HANGAR_TUNNEL_SERVER_NAME", ""),
		TLSCAFile:             env("HANGAR_TUNNEL_CA_FILE", ""),
		TLSInsecureSkipVerify: envBool("HANGAR_TUNNEL_INSECURE_SKIP_VERIFY", false),
		DialTimeout:           envDuration("HANGAR_TUNNEL_DIAL_TIMEOUT", 10*time.Second),
		HandshakeTimeout:      envDuration("HANGAR_TUNNEL_HANDSHAKE_TIMEOUT", 10*time.Second),
		BackoffMin:            envDuration("HANGAR_BACKOFF_MIN", time.Second),
		BackoffMax:            envDuration("HANGAR_BACKOFF_MAX", 30*time.Second),
		DrainTimeout:          envDuration("HANGAR_DRAIN_TIMEOUT", 60*time.Second),
		HealthAddr:            env("HANGAR_HEALTH_ADDR", ":8081"),
		InferenceEndpoint:     env("HANGAR_INFERENCE_ENDPOINT", ""),
		WorkloadNamespace:     env("HANGAR_WORKLOAD_NAMESPACE", "dynamo"),
		DiscoveryInterval:     envDuration("HANGAR_FRONTEND_DISCOVERY_INTERVAL", 0),
		LogFormat:             env("HANGAR_LOG_FORMAT", "json"),
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
	if c.Gateway == "" {
		return fmt.Errorf("HANGAR_TUNNEL_GATEWAY is required: the gateway's address, as host:port")
	}
	host, port, err := net.SplitHostPort(c.Gateway)
	if err != nil {
		return fmt.Errorf("HANGAR_TUNNEL_GATEWAY %q is not host:port: %w", c.Gateway, err)
	}
	if host == "" {
		return fmt.Errorf("HANGAR_TUNNEL_GATEWAY %q names no host", c.Gateway)
	}
	if _, err := strconv.Atoi(port); err != nil {
		return fmt.Errorf("HANGAR_TUNNEL_GATEWAY %q has no numeric port", c.Gateway)
	}
	if c.EnrolmentToken == "" {
		return fmt.Errorf("HANGAR_ENROLMENT_TOKEN is required")
	}
	if c.ConnsPerGateway < 1 {
		return fmt.Errorf("HANGAR_TUNNEL_CONNS_PER_GATEWAY must be positive")
	}
	if c.MaxConcurrentStreams < 0 || c.MaxConcurrentStreams > math.MaxUint32 {
		return fmt.Errorf("HANGAR_TUNNEL_MAX_CONCURRENT_STREAMS must be between 0 and %d", uint32(math.MaxUint32))
	}
	if c.DialTimeout <= 0 {
		return fmt.Errorf("HANGAR_TUNNEL_DIAL_TIMEOUT must be positive")
	}
	if c.HandshakeTimeout <= 0 {
		return fmt.Errorf("HANGAR_TUNNEL_HANDSHAKE_TIMEOUT must be positive")
	}
	if c.BackoffMin <= 0 {
		return fmt.Errorf("HANGAR_BACKOFF_MIN must be positive")
	}
	if c.BackoffMax < c.BackoffMin {
		return fmt.Errorf("HANGAR_BACKOFF_MAX must be >= HANGAR_BACKOFF_MIN")
	}
	if c.DrainTimeout <= 0 {
		return fmt.Errorf("HANGAR_DRAIN_TIMEOUT must be positive")
	}
	if c.HealthAddr == "" {
		return fmt.Errorf("HANGAR_HEALTH_ADDR must not be empty")
	}
	if c.InferenceEndpoint == "" && c.WorkloadNamespace == "" {
		return fmt.Errorf("HANGAR_WORKLOAD_NAMESPACE must not be empty unless HANGAR_INFERENCE_ENDPOINT is set")
	}
	if c.TLSCAFile != "" && !c.TLSEnabled {
		return fmt.Errorf("HANGAR_TUNNEL_CA_FILE is set but HANGAR_TUNNEL_TLS is off, so nothing would verify it")
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
