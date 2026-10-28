// Package config loads operator configuration from the environment.
package config

import (
	"errors"
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

	// HubURL is the outbound WebSocket endpoint on the control plane. Nothing ever connects
	// inward. wss:// is required unless HubAllowInsecure is set.
	HubURL string

	// HubAllowInsecure permits a plaintext ws:// hub URL. The enrolment token travels on that
	// connection as a bearer credential, so this is for local development only.
	HubAllowInsecure bool

	// EnrolmentToken is this cluster's enrolment token, presented as a bearer token on dial. The
	// hub binds it to one cluster id; rotate it by issuing a new one from the plane.
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

	// ObserveInterval is how often the operator re-reads its cluster's supply and sends what
	// changed. A slower interval only ages the hub's view; resync fills any hole.
	ObserveInterval time.Duration

	// ObserveTimeout bounds a single discovery.
	ObserveTimeout time.Duration

	// ObserveStaleAfter is how long readiness tolerates an observation loop that has stopped.
	// A wedged loop is otherwise invisible: the hub session stays up and the plane keeps
	// serving the last projection as current. Failing readiness turns it into a restart.
	ObserveStaleAfter time.Duration

	// ResyncInterval is how often the operator restates its whole observed inventory. It is what
	// retires an object the cluster stopped reporting.
	ResyncInterval time.Duration

	// BackoffMin and BackoffMax bound the reconnect backoff.
	BackoffMin time.Duration
	BackoffMax time.Duration

	// ReadOnly runs the operator as an observer: it reports what the cluster has, runs no
	// reconciler, and refuses every operation kind AllowedOperations does not name. This is how a
	// cluster is first pointed at a plane. Declared state still arrives and is stored in its CRDs,
	// so turning this off later starts from a complete view.
	ReadOnly bool

	// AllowedOperations are the operation kinds (cordon, drain, benchmark) a read-only operator
	// still performs. Empty, the default, means none. A list instead of a switch because the
	// kinds differ in blast radius: a benchmark runs an allowlisted harness image, a drain evicts
	// serving pods. Ignored when ReadOnly is false: a managed cluster already lets the plane
	// decide what runs in its workload namespace, so it performs every kind. The chart grants
	// the operator's RBAC on the same rule.
	AllowedOperations []string

	// LeaderElection enables controller-runtime leader election.
	LeaderElection bool

	// Namespace is where the operator stores CRDs and its leader-election lease.
	Namespace string

	// WorkloadNamespace is where rendered inference workloads go, kept apart from Namespace.
	WorkloadNamespace string

	// PoolLabelKey and GPUPoolValue name the node label that selects accelerator nodes. How a
	// cluster labels its pools is a local fact, not something the plane declares.
	PoolLabelKey string
	GPUPoolValue string

	// CachePoolValue, CacheTaintKey and CacheTaintValue name the pool weight prefetch jobs run
	// on and the taint that keeps other work off it. An empty value tolerates any value of the
	// key. Downloading is network and disk work, kept off the accelerator pool.
	CachePoolValue  string
	CacheTaintKey   string
	CacheTaintValue string

	// BenchmarkImages are the harness images a benchmark may run, matched exactly. The default
	// is the harness images the plane pins; a cluster that wants another adds it here.
	BenchmarkImages []string

	// PrefetchCPU, PrefetchMemory and PrefetchMemoryLimit size one weight download. Empty takes
	// the render's defaults.
	PrefetchCPU         string
	PrefetchMemory      string
	PrefetchMemoryLimit string

	// Multinode says whether this cluster can run a replica that spans machines. It needs Grove
	// or LeaderWorkerSet installed; without them Dynamo renders a single pod holding a fraction of
	// the devices, which hangs in collective initialisation. Off by default.
	Multinode bool

	// GPUTolerationKey and GPUTolerationValue are the taint GPU workers tolerate. The key
	// defaults to nvidia.com/gpu; an empty value tolerates any value of it.
	GPUTolerationKey   string
	GPUTolerationValue string

	// Fabric is the RDMA fabric workers move KV over: empty for none, "efa" for AWS Elastic
	// Fabric Adapter, or "rdma" for another RDMA device plugin. Without the fabric resource the
	// device plugin mounts no /dev/infiniband and NIXL falls back to TCP over the pod network.
	Fabric string

	// FabricDevices is how many fabric interfaces a worker pod asks for.
	FabricDevices int

	// FabricResource is the extended-resource name for those interfaces. Empty uses the AWS EFA
	// plugin's name under "efa"; "rdma" requires it.
	FabricResource string

	// FabricPrivileged runs fabric workers privileged with host IPC. Defaults to true for "efa",
	// whose GPU memory registration needs it, and false otherwise.
	FabricPrivileged bool

	// NIXLBackends names the NIXL plugins the KV connector registers. Empty keeps vLLM's default
	// of UCX; an EFA fabric wants LIBFABRIC, since EFA is a libfabric provider.
	NIXLBackends []string

	// AllowedImagePrefixes are the registries or repositories engine, frontend and prefetch
	// images must come from, matched at a path, tag or digest boundary. Empty, the default,
	// allows any image the plane names.
	AllowedImagePrefixes []string

	// EnginePullSecrets are Secrets in WorkloadNamespace the kubelet presents when pulling a
	// deployment's engine runtime image. Empty for public images.
	EnginePullSecrets []string

	// FrontendEnabled renders one OpenAI-compatible frontend per model in the model's own graph,
	// instead of relying on a single cluster-wide frontend. A frontend must tokenize every model
	// it serves, so one per model keeps a model that needs a different build from becoming every
	// model's problem. Off by default.
	FrontendEnabled bool

	// FrontendImage is this cluster's default frontend runtime. Empty falls back to the serving
	// block's engine image, which usually carries the frontend too.
	FrontendImage string

	// FrontendReplicas is how many pods one model's frontend runs. Two, so a rollout does not
	// take the model's only frontend with it.
	FrontendReplicas int

	// FrontendPullSecrets are Secrets in WorkloadNamespace the kubelet presents when pulling
	// FrontendImage, for a frontend image in a private registry.
	FrontendPullSecrets []string

	// FrontendCPU, FrontendMemory and FrontendMemoryLimit are what one frontend pod requests.
	// Empty takes the render's defaults. No CPU limit is set: throttling the frontend is latency
	// on the data path.
	FrontendCPU         string
	FrontendMemory      string
	FrontendMemoryLimit string

	// FrontendAnthropicAPI serves the Anthropic Messages API (`/v1/messages`) beside the OpenAI
	// one. FrontendAnthropicStripPreamble removes a client-injected system-prompt prefix that
	// varies per request and carries no instructions, so prompts from the same client share a
	// cacheable prefix. Both default on, matching the chart.
	FrontendAnthropicAPI           bool
	FrontendAnthropicStripPreamble bool

	// FrontendGlobalDynamoNamespace puts every component of a graph, frontend and workers, in
	// the shared Dynamo namespace. On by default; off narrows each graph to its own namespace.
	// Only read with FrontendEnabled: the cluster-wide frontend needs workers in the shared one.
	FrontendGlobalDynamoNamespace bool

	// DCGMEndpoint is the in-cluster DCGM exporter's metrics URL, scraped for device identity
	// only: the accelerator UUID Kubernetes does not carry (ADR 0007). Empty, the default,
	// disables the scrape and discovery falls back to annotated or synthetic ids.
	DCGMEndpoint string

	// DCGMSelector overrides the labels used to find the exporter pods behind DCGMEndpoint.
	// Empty means `app.kubernetes.io/name=<service>`, taken from the endpoint's own host.
	DCGMSelector string

	// DCGMTimeout bounds one scrape, short so a slow exporter cannot hold up an observation.
	DCGMTimeout time.Duration

	// ModelCachePVC is the shared volume holding downloaded weights, mounted at /models.
	ModelCachePVC string

	// LocalModelCacheDir is a directory on the node's own disk to mirror weights into before a
	// worker loads them, or empty to load from the shared volume. A shared network volume is the
	// place to keep weights, and its per-client throughput makes it a slow place to load them
	// from. A node that cannot hold the copy falls back to the shared path.
	LocalModelCacheDir string

	// InferenceEndpoint is the in-cluster URL a benchmark aims its load at. Empty, the normal
	// setting, finds the Service selecting Dynamo's Frontend component; set it only when the
	// frontend is something Dynamo did not label. The load stays inside the cluster.
	InferenceEndpoint string

	// BenchmarkArtifactBase is a URI prefix for a run's raw per-request records. Empty leaves
	// them in the job's pod and reports no URI.
	BenchmarkArtifactBase string

	// BenchmarkTimeout bounds a run that carried no deadline of its own.
	BenchmarkTimeout time.Duration

	// BenchmarkCPU and BenchmarkMemory are what one run's load generator requests. No CPU limit
	// is set, so the request is a floor: raise it where a high-concurrency run would otherwise
	// be starved and report its own latency as the engine's.
	BenchmarkCPU    string
	BenchmarkMemory string

	// BenchmarkTokenSecret names a Secret in WorkloadNamespace handed to benchmark jobs as
	// environment, for a tokenizer in a gated repository. Empty, the default, hands them none.
	BenchmarkTokenSecret string

	// legacyEFA records that the fabric came from WINGSMITH_EFA_DEVICES, for a startup warning.
	legacyEFA bool
}

// Load reads configuration from the environment, applying defaults and validating the result.
func Load() (*Config, error) {
	var l loader
	c := &Config{
		ClusterID:             env("WINGSMITH_CLUSTER_ID", ""),
		HubURL:                env("WINGSMITH_HUB_URL", ""),
		HubAllowInsecure:      l.bool("WINGSMITH_HUB_ALLOW_INSECURE", false),
		EnrolmentToken:        env("WINGSMITH_ENROLMENT_TOKEN", ""),
		TLSCAFile:             env("WINGSMITH_HUB_CA_FILE", ""),
		TLSInsecureSkipVerify: l.bool("WINGSMITH_HUB_INSECURE_SKIP_VERIFY", false),
		LogFormat:             env("WINGSMITH_LOG_FORMAT", "json"),
		HealthAddr:            env("WINGSMITH_HEALTH_ADDR", ":8081"),
		MetricsAddr:           env("WINGSMITH_METRICS_ADDR", "0"),
		SendQueueSize:         l.int("WINGSMITH_SEND_QUEUE_SIZE", 1024),
		HeartbeatInterval:     l.duration("WINGSMITH_HEARTBEAT_INTERVAL", 10*time.Second),
		LivenessTimeout:       l.duration("WINGSMITH_LIVENESS_TIMEOUT", 30*time.Second),
		ObserveInterval:       l.duration("WINGSMITH_OBSERVE_INTERVAL", 30*time.Second),
		ObserveTimeout:        l.duration("WINGSMITH_OBSERVE_TIMEOUT", 60*time.Second),
		ObserveStaleAfter:     l.duration("WINGSMITH_OBSERVE_STALE_AFTER", 5*time.Minute),
		ResyncInterval:        l.duration("WINGSMITH_RESYNC_INTERVAL", 15*time.Minute),
		BackoffMin:            l.duration("WINGSMITH_BACKOFF_MIN", time.Second),
		BackoffMax:            l.duration("WINGSMITH_BACKOFF_MAX", 30*time.Second),
		ReadOnly:              l.bool("WINGSMITH_READ_ONLY", false),
		AllowedOperations:     envList("WINGSMITH_ALLOWED_OPERATIONS"),
		LeaderElection:        l.bool("WINGSMITH_LEADER_ELECTION", true),
		Namespace:             env("WINGSMITH_NAMESPACE", "wingsmith-system"),
		WorkloadNamespace:     env("WINGSMITH_WORKLOAD_NAMESPACE", "dynamo"),
		PoolLabelKey:          env("WINGSMITH_POOL_LABEL_KEY", "wingsmith.dev/pool"),
		GPUPoolValue:          env("WINGSMITH_GPU_POOL_VALUE", "gpu"),
		CachePoolValue:        env("WINGSMITH_CACHE_POOL_VALUE", "cache"),
		CacheTaintKey:         env("WINGSMITH_CACHE_TAINT_KEY", "wingsmith.dev/cache"),
		CacheTaintValue:       env("WINGSMITH_CACHE_TAINT_VALUE", ""),
		BenchmarkImages:       envListOr("WINGSMITH_BENCHMARK_IMAGES", DefaultBenchmarkImages),
		PrefetchCPU:           env("WINGSMITH_PREFETCH_CPU", ""),
		PrefetchMemory:        env("WINGSMITH_PREFETCH_MEMORY", ""),
		PrefetchMemoryLimit:   env("WINGSMITH_PREFETCH_MEMORY_LIMIT", ""),
		ModelCachePVC:         env("WINGSMITH_MODEL_CACHE_PVC", "model-cache"),
		LocalModelCacheDir:    env("WINGSMITH_LOCAL_MODEL_CACHE_DIR", ""),
		EnginePullSecrets:     envList("WINGSMITH_ENGINE_PULL_SECRETS"),
		AllowedImagePrefixes:  envList("WINGSMITH_ALLOWED_IMAGE_PREFIXES"),

		FrontendEnabled:                l.bool("WINGSMITH_FRONTEND_ENABLED", false),
		FrontendImage:                  env("WINGSMITH_FRONTEND_IMAGE", ""),
		FrontendReplicas:               l.int("WINGSMITH_FRONTEND_REPLICAS", 2),
		FrontendPullSecrets:            envList("WINGSMITH_FRONTEND_PULL_SECRETS"),
		FrontendCPU:                    env("WINGSMITH_FRONTEND_CPU", ""),
		FrontendMemory:                 env("WINGSMITH_FRONTEND_MEMORY", ""),
		FrontendMemoryLimit:            env("WINGSMITH_FRONTEND_MEMORY_LIMIT", ""),
		FrontendAnthropicAPI:           l.bool("WINGSMITH_FRONTEND_ANTHROPIC_API", true),
		FrontendAnthropicStripPreamble: l.bool("WINGSMITH_FRONTEND_ANTHROPIC_STRIP_PREAMBLE", true),
		FrontendGlobalDynamoNamespace:  l.bool("WINGSMITH_FRONTEND_GLOBAL_DYNAMO_NAMESPACE", true),

		Multinode:             l.bool("WINGSMITH_MULTINODE", false),
		GPUTolerationKey:      env("WINGSMITH_GPU_TOLERATION_KEY", "nvidia.com/gpu"),
		GPUTolerationValue:    env("WINGSMITH_GPU_TOLERATION_VALUE", ""),
		Fabric:                strings.ToLower(env("WINGSMITH_FABRIC", "")),
		FabricDevices:         l.int("WINGSMITH_FABRIC_DEVICES", 0),
		FabricResource:        env("WINGSMITH_FABRIC_RESOURCE", ""),
		NIXLBackends:          envList("WINGSMITH_NIXL_BACKENDS"),
		InferenceEndpoint:     env("WINGSMITH_INFERENCE_ENDPOINT", ""),
		BenchmarkArtifactBase: env("WINGSMITH_BENCHMARK_ARTIFACT_BASE", ""),
		BenchmarkTimeout:      l.duration("WINGSMITH_BENCHMARK_TIMEOUT", 30*time.Minute),
		BenchmarkCPU:          env("WINGSMITH_BENCHMARK_CPU", "500m"),
		BenchmarkMemory:       env("WINGSMITH_BENCHMARK_MEMORY", "1Gi"),
		BenchmarkTokenSecret:  env("WINGSMITH_BENCHMARK_TOKEN_SECRET", ""),
		DCGMEndpoint:          env("WINGSMITH_DCGM_ENDPOINT", ""),
		DCGMSelector:          env("WINGSMITH_DCGM_SELECTOR", ""),
		DCGMTimeout:           l.duration("WINGSMITH_DCGM_TIMEOUT", 5*time.Second),
	}

	// WINGSMITH_EFA_DEVICES is the older spelling of an EFA fabric, what charts before 0.3.0 wrote.
	// Honoured only when WINGSMITH_FABRIC is unset, so a chart that has not moved to the new keys
	// keeps its fabric instead of quietly falling back to TCP.
	if efa := l.int("WINGSMITH_EFA_DEVICES", 0); efa != 0 {
		if c.Fabric != "" || c.FabricDevices != 0 {
			l.errs = append(l.errs, fmt.Errorf("WINGSMITH_EFA_DEVICES and WINGSMITH_FABRIC/WINGSMITH_FABRIC_DEVICES are both set; keep only WINGSMITH_FABRIC=efa with WINGSMITH_FABRIC_DEVICES"))
		} else {
			c.Fabric, c.FabricDevices, c.legacyEFA = "efa", efa, true
		}
	}
	c.FabricPrivileged = l.bool("WINGSMITH_FABRIC_PRIVILEGED", c.Fabric == "efa")

	lvl, err := parseLevel(env("WINGSMITH_LOG_LEVEL", "info"))
	if err != nil {
		l.errs = append(l.errs, err)
	}
	c.LogLevel = lvl

	// Every unreadable value is reported at once, and none falls back to its default: a
	// WINGSMITH_READ_ONLY=yes that quietly meant false would start a read-write operator.
	if err := errors.Join(l.errs...); err != nil {
		return nil, err
	}

	if err := c.validate(); err != nil {
		return nil, err
	}
	return c, nil
}

func (c *Config) validate() error {
	if c.ClusterID == "" {
		return fmt.Errorf("WINGSMITH_CLUSTER_ID is required")
	}
	if c.HubURL == "" {
		return fmt.Errorf("WINGSMITH_HUB_URL is required")
	}
	u, err := url.Parse(c.HubURL)
	if err != nil {
		return fmt.Errorf("WINGSMITH_HUB_URL is not a URL: %w", err)
	}
	switch u.Scheme {
	case "wss":
	case "ws":
		if !c.HubAllowInsecure {
			return fmt.Errorf("WINGSMITH_HUB_URL is plaintext ws://, which sends the enrolment token unencrypted; " +
				"use wss://, or set WINGSMITH_HUB_ALLOW_INSECURE=true for local development")
		}
	default:
		return fmt.Errorf("WINGSMITH_HUB_URL scheme must be wss (or ws with WINGSMITH_HUB_ALLOW_INSECURE=true), got %q", u.Scheme)
	}
	if c.SendQueueSize < 1 {
		return fmt.Errorf("WINGSMITH_SEND_QUEUE_SIZE must be positive")
	}
	if c.LivenessTimeout <= c.HeartbeatInterval {
		return fmt.Errorf("WINGSMITH_LIVENESS_TIMEOUT (%s) must exceed WINGSMITH_HEARTBEAT_INTERVAL (%s)", c.LivenessTimeout, c.HeartbeatInterval)
	}
	if c.ObserveInterval <= 0 {
		return fmt.Errorf("WINGSMITH_OBSERVE_INTERVAL must be positive")
	}
	if c.ObserveTimeout <= 0 {
		return fmt.Errorf("WINGSMITH_OBSERVE_TIMEOUT must be positive")
	}
	if c.ResyncInterval <= 0 {
		return fmt.Errorf("WINGSMITH_RESYNC_INTERVAL must be positive")
	}
	if c.ObserveStaleAfter <= c.ObserveInterval {
		return fmt.Errorf("WINGSMITH_OBSERVE_STALE_AFTER (%s) must exceed WINGSMITH_OBSERVE_INTERVAL (%s)", c.ObserveStaleAfter, c.ObserveInterval)
	}
	if c.BackoffMax < c.BackoffMin {
		return fmt.Errorf("WINGSMITH_BACKOFF_MAX must be >= WINGSMITH_BACKOFF_MIN")
	}
	if c.WorkloadNamespace == "" {
		return fmt.Errorf("WINGSMITH_WORKLOAD_NAMESPACE must not be empty")
	}
	if c.PoolLabelKey == "" {
		return fmt.Errorf("WINGSMITH_POOL_LABEL_KEY must not be empty")
	}
	// Zero would render a frontend that exists and serves nothing, which is a deployment
	// reachable by nothing and healthy in every reading of it. Turning the frontend off is
	// WINGSMITH_FRONTEND_ENABLED and not a replica count, so say so instead of quietly substituting
	// the default for what somebody wrote.
	if c.FrontendEnabled && c.FrontendReplicas < 1 {
		return fmt.Errorf("WINGSMITH_FRONTEND_REPLICAS must be at least 1; set WINGSMITH_FRONTEND_ENABLED=false to render no frontend")
	}
	switch c.Fabric {
	case "", "efa", "rdma":
	default:
		return fmt.Errorf("WINGSMITH_FABRIC must be empty, efa or rdma, got %q", c.Fabric)
	}
	if c.FabricDevices < 0 {
		return fmt.Errorf("WINGSMITH_FABRIC_DEVICES must not be negative")
	}
	if c.Fabric == "" && c.FabricDevices > 0 {
		return fmt.Errorf("WINGSMITH_FABRIC_DEVICES is set but WINGSMITH_FABRIC is not; name the fabric (efa or rdma)")
	}
	if c.Fabric != "" && c.FabricDevices == 0 {
		return fmt.Errorf("WINGSMITH_FABRIC=%s requests no interfaces; set WINGSMITH_FABRIC_DEVICES", c.Fabric)
	}
	if c.Fabric == "rdma" && strings.TrimSpace(c.FabricResource) == "" {
		return fmt.Errorf("WINGSMITH_FABRIC=rdma needs WINGSMITH_FABRIC_RESOURCE, the device plugin's resource name")
	}
	if c.LogFormat != "json" && c.LogFormat != "text" {
		return fmt.Errorf("WINGSMITH_LOG_FORMAT must be json or text")
	}
	return nil
}

// Warnings are settings that are valid but weaken the hub connection or are spelled the old way,
// for logging at startup.
func (c *Config) Warnings() []string {
	var out []string
	if strings.HasPrefix(c.HubURL, "ws://") {
		out = append(out, "WINGSMITH_HUB_URL is plaintext ws://; the enrolment token is sent unencrypted")
	}
	if c.TLSInsecureSkipVerify {
		out = append(out, "WINGSMITH_HUB_INSECURE_SKIP_VERIFY is on; the hub certificate is not verified")
	}
	if c.legacyEFA {
		out = append(out, "WINGSMITH_EFA_DEVICES is read as WINGSMITH_FABRIC=efa with WINGSMITH_FABRIC_DEVICES; set those instead")
	}
	return out
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
		return 0, fmt.Errorf("WINGSMITH_LOG_LEVEL %q is not one of debug, info, warn, error", s)
	}
}

func env(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return def
}

// DefaultBenchmarkImages are the harness images the plane pins for aiperf and evalscope.
var DefaultBenchmarkImages = []string{
	"nvcr.io/nvidia/ai-dynamo/aiperf:0.12.0",
	"ghcr.io/wingsmith-dev/evalscope:1.11.1",
}

// envListOr is envList with a default for an unset key.
func envListOr(key string, def []string) []string {
	if out := envList(key); out != nil {
		return out
	}
	return append([]string(nil), def...)
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

// loader reads typed values and collects every one it could not parse.
type loader struct {
	errs []error
}

// lookup returns a set, non-empty value. Empty is how a Helm template renders an omitted field.
func lookup(key string) (string, bool) {
	v, ok := os.LookupEnv(key)
	if !ok {
		return "", false
	}
	v = strings.TrimSpace(v)
	return v, v != ""
}

func (l *loader) bool(key string, def bool) bool {
	v, ok := lookup(key)
	if !ok {
		return def
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		l.errs = append(l.errs, fmt.Errorf("%s=%q is not a boolean; use true or false", key, v))
		return def
	}
	return b
}

func (l *loader) int(key string, def int) int {
	v, ok := lookup(key)
	if !ok {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		l.errs = append(l.errs, fmt.Errorf("%s=%q is not an integer", key, v))
		return def
	}
	return n
}

func (l *loader) duration(key string, def time.Duration) time.Duration {
	v, ok := lookup(key)
	if !ok {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		l.errs = append(l.errs, fmt.Errorf("%s=%q is not a duration such as 30s or 5m", key, v))
		return def
	}
	return d
}
