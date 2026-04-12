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

	// ReadOnly runs the operator as an observer: it reports what the cluster has, and declines
	// every path that would write to it.
	//
	// This is how a cluster is first pointed at a plane. Without it, nothing writes either —
	// but only because no deployment has been declared yet, which is safety by emptiness and
	// lasts exactly until someone declares one. The two write paths are the reconciler, which
	// renders declared state into Kubernetes objects, and the operations executor, which
	// cordons, drains and benchmarks; both are refused here unless AllowedOperations names one.
	//
	// Declared state still arrives and is still stored in its CRDs. The local copy is inert
	// without the controller that reads it, and keeping it means turning this off later starts
	// from a complete view rather than waiting for the next resync.
	ReadOnly bool

	// AllowedOperations are the operation kinds a read-only operator will still perform.
	//
	// It exists because "read-only" was one word for two different guarantees, and a cluster
	// turned out to want them separately: the plane may not *own* what runs here — no
	// reconciler, nothing converged, nothing taken over — and may still be asked for specific,
	// bounded things. Benchmarking is the case that forced the split. It writes to the cluster,
	// so refusing it under a blanket read-only was right, but the alternative was turning on
	// reconciliation to get it, which is a far larger decision than "measure this".
	//
	// An allowlist and not a second boolean, because the kinds are not interchangeable. A
	// benchmark is additive, self-limiting and disposable; a drain on a cluster this plane does
	// not manage is destructive and hard to undo. A boolean would have repeated exactly the
	// mistake ReadOnly made, one level down.
	//
	// Empty is the default and preserves the old behaviour precisely: a read-only operator that
	// refuses everything. Widening is opt-in per kind and per cluster, which is what keeps this
	// from quietly granting an existing cluster more than it had.
	//
	// Ignored when ReadOnly is false — an operator that is not read-only performs every kind it
	// implements, and a second gate there would be a second meaning again.
	AllowedOperations []string

	// LeaderElection enables controller-runtime leader election.
	LeaderElection bool

	// Namespace is where the operator stores CRDs and its leader-election lease.
	Namespace string

	// WorkloadNamespace is where rendered inference workloads go. It is separate from Namespace
	// so that a cluster admin can keep the operator's own state away from what it renders.
	WorkloadNamespace string

	// PoolLabelKey and GPUPoolValue name the node label that selects accelerator nodes. This is
	// a per-cluster convention rather than anything the plane declares: the plane chooses the
	// cluster and the device class, and how this cluster happens to label its GPU nodes is a
	// local fact the operator is configured with.
	PoolLabelKey string
	GPUPoolValue string

	// CachePoolValue and CacheTaintKey name the pool the weight prefetch job runs on and the
	// taint that keeps everything else off it. Downloading weights is network and disk work, so
	// it is deliberately not scheduled onto the accelerator pool where it would hold a GPU idle.
	CachePoolValue string
	CacheTaintKey  string

	// PrefetchCPU and PrefetchMemory are what one weight download asks the scheduler for. Empty
	// takes the render's own default, which is sized to fit an ordinary node rather than a
	// dedicated cache one.
	PrefetchCPU    string
	PrefetchMemory string

	// DCGMEndpoint is the in-cluster DCGM exporter's metrics URL, which the observation loop
	// scrapes for device identity — the true accelerator UUID, which Kubernetes does not carry
	// anywhere (ADR 0007). It is read cluster-locally and only for identity; the measurements on
	// those same series are telemetry and never enter the sync stream. Empty disables the
	// scrape, leaving discovery on the annotation and synthetic-id fallbacks.
	DCGMEndpoint string

	// DCGMSelector overrides the labels used to find the exporter pods behind DCGMEndpoint.
	// Empty means `app.kubernetes.io/name=<service>`, taken from the endpoint's own host.
	DCGMSelector string

	// DCGMTimeout bounds one scrape. It is short on purpose: an exporter that is slow must not
	// hold up an observation tick, because a late snapshot ages every node the plane can see.
	DCGMTimeout time.Duration

	// ModelCachePVC is the shared volume holding downloaded weights, mounted at /models so that
	// replicas of one model pull it once between them rather than once each.
	ModelCachePVC string

	// InferenceEndpoint is the in-cluster URL of the shared OpenAI-compatible frontend, which is
	// what a benchmark points its load at.
	//
	// A per-cluster convention rather than anything the plane declares, for the same reason the
	// pool labels are: the plane says which deployment to measure and the model name to ask for,
	// and how this cluster happens to expose its frontend is a local fact. It is also the line
	// rule 4 draws — the load is generated inside the cluster and aimed at a cluster-local
	// address, so no inference traffic crosses the control plane even while it is being measured.
	//
	// Empty is the normal setting, and it means the operator finds the frontend by asking the
	// cluster for the Service that selects Dynamo's Frontend component. This used to default to
	// `dynamo-frontend`, a name no cluster we run actually uses, so every benchmark failed on a
	// DNS error that named the address and not the setting behind it. Set this only when a
	// cluster fronts its models with something Dynamo did not label.
	InferenceEndpoint string

	// BenchmarkArtifactBase is where a run's raw per-request records are written, as a URI
	// prefix. Those records are telemetry — one row per request, never joined — so they go to
	// object storage directly and only the URI travels the sync stream. Empty leaves them in the
	// job's pod and reports no URI, which is the default because it needs no credentials.
	BenchmarkArtifactBase string

	// BenchmarkTimeout bounds a run that reported no deadline of its own. A load generator that
	// wedged against an endpoint which stopped answering would otherwise hold its operation open
	// forever.
	BenchmarkTimeout time.Duration

	// BenchmarkCPU and BenchmarkMemory are what one run's load generator requests. They are
	// per-cluster because the right answer depends on what the cluster has spare: the defaults
	// are small enough to schedule onto a modest node pool, which is the behaviour a cluster
	// that has never thought about this should get.
	//
	// No CPU limit is ever set, so the request is a floor and not a cap — a run bursts into
	// whatever the node has idle. Raising the request is how you stop a high-concurrency run
	// from being co-scheduled with work that starves it, which matters because a load generator
	// short of CPU does not fail, it reports latency that is its own rather than the engine's.
	BenchmarkCPU    string
	BenchmarkMemory string
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
		ReadOnly:              envBool("HANGAR_READ_ONLY", false),
		AllowedOperations:     envList("HANGAR_ALLOWED_OPERATIONS"),
		LeaderElection:        envBool("HANGAR_LEADER_ELECTION", true),
		Namespace:             env("HANGAR_NAMESPACE", "hangar-system"),
		WorkloadNamespace:     env("HANGAR_WORKLOAD_NAMESPACE", "dynamo"),
		PoolLabelKey:          env("HANGAR_POOL_LABEL_KEY", "hangar.io/pool"),
		GPUPoolValue:          env("HANGAR_GPU_POOL_VALUE", "gpu"),
		CachePoolValue:        env("HANGAR_CACHE_POOL_VALUE", "cache"),
		CacheTaintKey:         env("HANGAR_CACHE_TAINT_KEY", "hangar.io/cache"),
		PrefetchCPU:           env("HANGAR_PREFETCH_CPU", ""),
		PrefetchMemory:        env("HANGAR_PREFETCH_MEMORY", ""),
		ModelCachePVC:         env("HANGAR_MODEL_CACHE_PVC", "model-cache"),
		InferenceEndpoint:     env("HANGAR_INFERENCE_ENDPOINT", ""),
		BenchmarkArtifactBase: env("HANGAR_BENCHMARK_ARTIFACT_BASE", ""),
		BenchmarkTimeout:      envDuration("HANGAR_BENCHMARK_TIMEOUT", 30*time.Minute),
		BenchmarkCPU:          env("HANGAR_BENCHMARK_CPU", "500m"),
		BenchmarkMemory:       env("HANGAR_BENCHMARK_MEMORY", "1Gi"),
		DCGMEndpoint:          env("HANGAR_DCGM_ENDPOINT", "http://dcgm-exporter.platform.svc.cluster.local:9400/metrics"),
		DCGMSelector:          env("HANGAR_DCGM_SELECTOR", ""),
		DCGMTimeout:           envDuration("HANGAR_DCGM_TIMEOUT", 5*time.Second),
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
	if c.WorkloadNamespace == "" {
		return fmt.Errorf("HANGAR_WORKLOAD_NAMESPACE must not be empty")
	}
	if c.PoolLabelKey == "" {
		return fmt.Errorf("HANGAR_POOL_LABEL_KEY must not be empty")
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

// envList reads a comma-separated list, dropping blanks and surrounding space so that a
// ConfigMap value written across a line, or with a trailing comma, means what it looks like.
func envList(key string) []string {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return nil
	}
	var out []string
	for _, part := range strings.Split(raw, ",") {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
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
