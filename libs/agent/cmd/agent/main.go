// Command agent runs hangar's agent: one Deployment per GPU cluster, dialling out to the gateway and
// forwarding whatever arrives to the cluster's own OpenAI-compatible Dynamo frontend Service.
//
// It is given one address and redials it until it holds a tunnel to every gateway replica, because
// the gateway tells it in the handshake how many there are. A cluster is therefore never configured
// with the shape of the control plane, which is the whole point: the plane can scale its gateway
// without an edit reaching a single cluster.
//
// It is the data plane's cluster-local half and is deliberately not the operator, the other hangar
// process a cluster runs: the operator is a leader-elected singleton on the control path, this
// scales with traffic, and neither shares a process or a connection with the other.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/hangar-dev/hangar/libs/agent/internal/agent"
	"github.com/hangar-dev/hangar/libs/agent/internal/dynamo"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// version is stamped at build time with -ldflags.
var version = "dev"

func main() {
	if err := run(); err != nil {
		slog.Error("agent exited", "err", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := agent.Load()
	if err != nil {
		return err
	}
	log := newLogger(cfg).With("cluster_id", cfg.ClusterID, "version", version)
	slog.SetDefault(log)
	for _, warning := range cfg.Warnings() {
		log.Warn("agent: " + warning)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	resolver, err := newResolver(cfg)
	if err != nil {
		return err
	}

	// BaseContext is deliberately not ctx. Cancelling it aborts every handler mid-stream, and
	// SIGTERM has to drain instead: the server's Shutdown sends GOAWAY and lets the streams
	// already accepted finish, each of which is a generation somebody is reading.
	server, err := tunnel.NewConnector(tunnel.ConnectorConfig{
		Handler:     dynamo.NewHandler(resolver, tunnel.InnerHopTimeout(cfg.ResponseHeaderTimeout)),
		Limits:      tunnel.Limits{MaxConcurrentStreams: uint32(cfg.MaxConcurrentStreams)}, //nolint:gosec // bounded by validate
		BaseContext: context.Background(),
	})
	if err != nil {
		return err
	}

	dialler, err := agent.NewDialler(cfg)
	if err != nil {
		return err
	}

	metrics := agent.NewMetrics()
	metrics.WatchDiscovery(resolver.DiscoveryAge)
	supervisor, err := agent.NewSupervisor(agent.SupervisorConfig{
		Gateway:         cfg.Gateway,
		ConnsPerGateway: cfg.ConnsPerGateway,
		Dial:            dialler.Dial,
		Server:          server,
		BackoffMin:      cfg.BackoffMin,
		BackoffMax:      cfg.BackoffMax,
		Metrics:         metrics,
		Log:             log,
	})
	if err != nil {
		return err
	}

	probes := agent.NewProbeServer(agent.ProbeConfig{
		Addr:     cfg.HealthAddr,
		Ready:    readiness(supervisor.Ready, resolver.DiscoveryAge, cfg.DiscoveryStaleAfter),
		Coverage: supervisor.Coverage,
		Expected: supervisor.Expected,
		Metrics:  metrics,
	})

	var wg sync.WaitGroup
	wg.Add(3)
	go func() {
		defer wg.Done()
		resolver.Run(ctx)
	}()
	go func() {
		defer wg.Done()
		if err := probes.Run(ctx); err != nil {
			log.Error("agent: probe listener stopped", "err", err)
		}
	}()

	supervised := make(chan struct{})
	go func() {
		defer wg.Done()
		defer close(supervised)
		supervisor.Run(ctx)
	}()

	log.Info("agent: dialling out",
		"gateway", cfg.Gateway,
		"conns_per_gateway", cfg.ConnsPerGateway,
		"health", cfg.HealthAddr,
		"response_header_timeout", tunnel.InnerHopTimeout(cfg.ResponseHeaderTimeout))

	<-ctx.Done()
	log.Info("agent: draining", "tunnels", supervisor.Tunnels(), "timeout", cfg.DrainTimeout)

	drain, cancel := context.WithTimeout(context.WithoutCancel(ctx), cfg.DrainTimeout)
	defer cancel()
	if err := server.Shutdown(drain); err != nil {
		log.Warn("agent: drain did not finish, severing what is left", "err", err)
		_ = server.Close()
	}
	<-supervised
	wg.Wait()
	log.Info("agent: stopped")
	return nil
}

// readiness is the agent's readiness probe: the supervisor's own check, then frontend discovery
// having heard from the API server within staleAfter.
//
// Stale discovery does not stop the agent forwarding; it keeps the addresses it last saw, which is
// right through an API server blip. Past staleAfter those addresses may no longer be where the
// frontends are, and a failing probe is how a person finds out before the requests do.
func readiness(tunnels func() error, discoveryAge func() time.Duration, staleAfter time.Duration) func() error {
	return func() error {
		if err := tunnels(); err != nil {
			return err
		}
		if staleAfter <= 0 {
			return nil
		}
		if age := discoveryAge(); age > staleAfter {
			return fmt.Errorf("frontend discovery has had no answer from the API server for %s", age.Round(time.Second))
		}
		return nil
	}
}

// newResolver builds the frontend lookup: the configured override if there is one, otherwise a
// discovery loop against this cluster's API server.
//
// The Kubernetes client is only built when it is needed, so an agent given an explicit endpoint
// runs with no cluster credentials at all.
func newResolver(cfg *agent.Config) (*dynamo.Resolver, error) {
	frontendCfg := dynamo.Config{
		Endpoint:  cfg.InferenceEndpoint,
		Namespace: cfg.WorkloadNamespace,
		Interval:  cfg.DiscoveryInterval,
	}
	if cfg.InferenceEndpoint == "" {
		restCfg, err := kubeConfig()
		if err != nil {
			return nil, err
		}
		clientset, err := kubernetes.NewForConfig(restCfg)
		if err != nil {
			return nil, fmt.Errorf("build a Kubernetes client: %w", err)
		}
		frontendCfg.Services = clientset.CoreV1().Services(cfg.WorkloadNamespace)
	}
	return dynamo.New(frontendCfg)
}

// kubeConfig reads the in-cluster service account.
//
// In-cluster only, and no kubeconfig fallback: the agent is a Deployment in the cluster it
// serves, and a process running anywhere else has no business discovering that cluster's frontend by
// name — it would resolve to a ClusterIP it cannot reach. Running one from a laptop means setting
// HANGAR_INFERENCE_ENDPOINT, which skips discovery entirely.
func kubeConfig() (*rest.Config, error) {
	inCluster, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("no in-cluster credentials; set HANGAR_INFERENCE_ENDPOINT to skip frontend discovery: %w", err)
	}
	return inCluster, nil
}

func newLogger(cfg *agent.Config) *slog.Logger {
	opts := &slog.HandlerOptions{Level: cfg.LogLevel}
	if cfg.LogFormat == "text" {
		return slog.New(slog.NewTextHandler(os.Stderr, opts))
	}
	return slog.New(slog.NewJSONHandler(os.Stderr, opts))
}
