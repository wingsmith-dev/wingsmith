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

	// ObserveTimeout bounds a single discovery. It must exceed nothing in particular, but a
	// value below ObserveInterval means a slow cluster read is reported as a failure rather
	// than simply arriving late.
	ObserveTimeout time.Duration

	// ObserveStaleAfter is how long the readiness probe tolerates a loop that has stopped
	// observing before it reports the operator unready.
	//
	// A wedged observation loop is otherwise undetectable from outside: the process does not
	// exit, the hub session stays up on its own goroutine, and the plane keeps serving the last
	// projection as current. Failing readiness is what turns that into a restart.
	ObserveStaleAfter time.Duration

	// ResyncInterval is how often the operator restates its whole observed inventory unprompted.
	// It is the only thing that retires an object the cluster has stopped reporting, so it is
	// the upper bound on how long a departed node lingers in the plane.
	ResyncInterval time.Duration

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
	PrefetchCPU         string
	PrefetchMemory      string
	PrefetchMemoryLimit string

	// Multinode says whether this cluster can run a replica that spans machines.
	//
	// Off by default, and the default is the safe one. A multi-node replica is a leader-worker
	// group, which Dynamo renders only where Grove or LeaderWorkerSet is installed; neither is
	// installed by the chart at its own defaults. Turning this on without them produces a single
	// pod holding a fraction of the devices the engine expects, which hangs in collective
	// initialisation rather than failing. A cluster sets it once it has installed one of them.
	Multinode bool

	// EFADevices is how many Elastic Fabric Adapter interfaces a worker pod asks for.
	//
	// A per-cluster fact, like Multinode: the resource only exists where the AWS EFA device
	// plugin is installed and the instance type carries the NICs. Zero means ask for none, which
	// is what every cluster did before this existed.
	//
	// It is not cosmetic. Without the resource the plugin does not mount /dev/infiniband into the
	// container, so NIXL's UCX layer finds no RDMA transport and falls back to TCP over the pod
	// network. Prefill/decode disaggregation moves the whole KV cache of every request across
	// that link -- on Kimi K3 at 38k input tokens it is ~1.8GB per request -- and on TCP it ran
	// at ~300MB/s, 6.1s per transfer, against a fabric that should do far better. Aggregated
	// serving never crosses a node for KV and so was never affected, which is precisely what made
	// the resulting benchmark comparison look like a verdict on disaggregation.
	EFADevices int

	// EnginePullSecrets are Secrets in WorkloadNamespace the kubelet presents when pulling a
	// deployment's EngineRuntimeImage. Empty for the anonymous NVIDIA runtime; set on clusters
	// that run private engine builds such as the GLM 5.3 runtime in GHCR.
	//
	// Keeps its name and its HANGAR_ENGINE_PULL_SECRETS env var through the rename of
	// engine_version to engine_runtime_version: no version is involved, `engine` is still a
	// real concept, and renaming a Helm value would silently drop the credential on any
	// cluster that had set it -- an ImagePullBackOff bought for nothing.
	EnginePullSecrets []string

	// FrontendEnabled renders one OpenAI-compatible frontend per model, beside the worker in the
	// same graph, instead of relying on the single frontend `cluster-setup` installs.
	//
	// Off by default, and the default is the migration control rather than a preference. With it
	// off the operator renders exactly what it always did, byte for byte, so this binary can
	// reach every cluster before any cluster changes shape; a cluster moves when somebody sets
	// this, and moves back by unsetting it. The reason to move is that a frontend is a binary
	// which must tokenize and register every model reaching it, so one per cluster makes the
	// image a fleet-wide lowest common denominator — and one model that needs a newer or patched
	// build makes the whole fleet's frontend that model's problem.
	FrontendEnabled bool

	// FrontendImage is this cluster's default frontend runtime. Empty falls back to the serving
	// block's engine image, which is usually the same build — the Dynamo runtime carries the
	// frontend and the engines together — and a deployment that needs a different one says so
	// itself.
	FrontendImage string

	// FrontendReplicas is how many pods one model's frontend runs. Two, so that a rollout does
	// not take the model's only frontend with it: the frontend holds no weights, so the second
	// replica is cheap and the outage it avoids is not.
	FrontendReplicas int

	// FrontendPullSecrets are Secrets in WorkloadNamespace the kubelet presents when pulling
	// FrontendImage. Empty for the nvcr.io default, which is served anonymously.
	//
	// It exists because a frontend image is not always NVIDIA's: the Kimi K3 fix is a patched
	// runtime in a private GHCR repository, and a private pull with no credential is an
	// ImagePullBackOff that nothing reports upward. A pull Secret is namespaced and the kubelet
	// reads it from the pod's own namespace, so these name Secrets where the workloads are, not
	// where this operator is.
	FrontendPullSecrets []string

	// FrontendCPU, FrontendMemory and FrontendMemoryLimit are what one frontend pod requests.
	// Empty takes the render's defaults, sized to what the cluster frontend has been observed to
	// run inside. No CPU limit is ever set: the frontend copies every token of every response,
	// so throttling it is latency on the data path.
	FrontendCPU         string
	FrontendMemory      string
	FrontendMemoryLimit string

	// FrontendAnthropicAPI serves the Anthropic Messages API (`/v1/messages`) beside the OpenAI
	// surface, and FrontendAnthropicStripPreamble drops the Claude Code billing preamble from
	// the system prompt. Both default on, matching the chart: the first because a cluster
	// without it cannot be used by Anthropic-protocol clients at all and the alternative is a
	// translating proxy per client, the second because a prompt that starts the same way every
	// time is a prompt the prefix cache can hit.
	FrontendAnthropicAPI           bool
	FrontendAnthropicStripPreamble bool

	// FrontendGlobalDynamoNamespace puts the rendered frontend in the shared Dynamo namespace,
	// which is what every frontend and every worker on every cluster we run does today.
	//
	// On by default because that is the observed behaviour and not because it is settled.
	// Whether a frontend can be narrowed to its own model's workers is unresolved and needs an
	// experiment on a live cluster; this exists so that the answer is a value somebody sets on
	// one cluster rather than a change to the renderer.
	FrontendGlobalDynamoNamespace bool

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

	// LocalModelCacheDir is a directory on the node's own disk to mirror the weights into, or
	// empty to read them from the shared volume as before.
	//
	// The shared volume is the right place to *keep* weights and the wrong place to *load* them
	// from. Measured on a p6-b300.48xlarge reading Kimi-K3 over EFS: 1.47 GB/s per node, with
	// half of each NFS READ's latency spent queued on the client rather than waiting on the
	// server. Four nodes loading at once cost 19% over one loading alone, which says the ceiling
	// is per-client and not the filesystem — so it is not a ceiling more replicas can share
	// their way out of, and 1.4 TB against it is ~16 minutes of a GPU node doing nothing.
	//
	// Off unless a cluster names a path, because it is only worth anything where the node has a
	// disk large enough to hold a model, and that is a fact about the machine that nothing here
	// can see. A cluster that sets it and cannot honour it still serves: the mirror is a cache,
	// and `localizeContainer` falls back to the shared path rather than failing the pod.
	LocalModelCacheDir string

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
		ObserveTimeout:        envDuration("HANGAR_OBSERVE_TIMEOUT", 60*time.Second),
		ObserveStaleAfter:     envDuration("HANGAR_OBSERVE_STALE_AFTER", 5*time.Minute),
		ResyncInterval:        envDuration("HANGAR_RESYNC_INTERVAL", 15*time.Minute),
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
		PrefetchMemoryLimit:   env("HANGAR_PREFETCH_MEMORY_LIMIT", ""),
		ModelCachePVC:         env("HANGAR_MODEL_CACHE_PVC", "model-cache"),
		LocalModelCacheDir:    env("HANGAR_LOCAL_MODEL_CACHE_DIR", ""),
		EnginePullSecrets:     envList("HANGAR_ENGINE_PULL_SECRETS"),

		FrontendEnabled:                envBool("HANGAR_FRONTEND_ENABLED", false),
		FrontendImage:                  env("HANGAR_FRONTEND_IMAGE", ""),
		FrontendReplicas:               envInt("HANGAR_FRONTEND_REPLICAS", 2),
		FrontendPullSecrets:            envList("HANGAR_FRONTEND_PULL_SECRETS"),
		FrontendCPU:                    env("HANGAR_FRONTEND_CPU", ""),
		FrontendMemory:                 env("HANGAR_FRONTEND_MEMORY", ""),
		FrontendMemoryLimit:            env("HANGAR_FRONTEND_MEMORY_LIMIT", ""),
		FrontendAnthropicAPI:           envBool("HANGAR_FRONTEND_ANTHROPIC_API", true),
		FrontendAnthropicStripPreamble: envBool("HANGAR_FRONTEND_ANTHROPIC_STRIP_PREAMBLE", true),
		FrontendGlobalDynamoNamespace:  envBool("HANGAR_FRONTEND_GLOBAL_DYNAMO_NAMESPACE", true),

		Multinode:             envBool("HANGAR_MULTINODE", false),
		EFADevices:            envInt("HANGAR_EFA_DEVICES", 0),
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
	if c.ObserveTimeout <= 0 {
		return fmt.Errorf("HANGAR_OBSERVE_TIMEOUT must be positive")
	}
	if c.ResyncInterval <= 0 {
		return fmt.Errorf("HANGAR_RESYNC_INTERVAL must be positive")
	}
	if c.ObserveStaleAfter <= c.ObserveInterval {
		return fmt.Errorf("HANGAR_OBSERVE_STALE_AFTER (%s) must exceed HANGAR_OBSERVE_INTERVAL (%s)", c.ObserveStaleAfter, c.ObserveInterval)
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
	// Zero would render a frontend that exists and serves nothing, which is a deployment
	// reachable by nothing and healthy in every reading of it. Turning the frontend off is
	// HANGAR_FRONTEND_ENABLED and not a replica count, so say so rather than quietly substitute
	// the default for what somebody wrote.
	if c.FrontendEnabled && c.FrontendReplicas < 1 {
		return fmt.Errorf("HANGAR_FRONTEND_REPLICAS must be at least 1; set HANGAR_FRONTEND_ENABLED=false to render no frontend")
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
