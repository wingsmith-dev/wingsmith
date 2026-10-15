// Command operator runs the Hangar cluster operator: one instance per Kubernetes cluster, dialling
// out to the control-plane hub and reconciling CRDs into cluster objects.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"golang.org/x/sync/errgroup"

	"k8s.io/client-go/discovery"

	ctrl "sigs.k8s.io/controller-runtime"
	ctrlclient "sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/manager"

	"github.com/hangar-dev/hangar/libs/operator/internal/checkpoint"
	"github.com/hangar-dev/hangar/libs/operator/internal/config"
	"github.com/hangar-dev/hangar/libs/operator/internal/health"
	"github.com/hangar-dev/hangar/libs/operator/internal/observe"
	"github.com/hangar-dev/hangar/libs/operator/internal/operate"
	"github.com/hangar-dev/hangar/libs/operator/internal/reconcile"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
)

// version is stamped at build time with -ldflags.
var version = "dev"

func main() {
	if err := run(); err != nil {
		slog.Error("operator exited", "error", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	log := newLogger(cfg).With("cluster_id", cfg.ClusterID, "version", version)
	slog.SetDefault(log)
	for _, warning := range cfg.Warnings() {
		log.Warn(warning)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	mgr, err := reconcile.NewManager(cfg, log)
	if err != nil {
		return err
	}
	// The manager runs either way. Its cached client is what the desired-state store reads and
	// writes, and that store keeps working read-only — what stops is the controller that would
	// turn those CRDs into Kubernetes objects, which is simply never registered.
	// One scrape of the exporters, read by both loops. The observation loop publishes the
	// pod-to-accelerator binding it already read; the reconcile loop uses it to say which cards
	// each replica holds. Scraping twice would double the load on every exporter in the cluster
	// and still give two readings that disagree.
	deviceIndex := &observe.DeviceIndex{}

	// What the cluster can do, read once per observation tick and gated on by the reconcile
	// loop. Shared for the same reason deviceIndex is: the fact is discovered on one loop's
	// clock and acted on by the other's.
	capabilities := &observe.Capability{}

	var reconciler *reconcile.SyncedResourceReconciler
	if cfg.ReadOnly {
		// Read-only renders and tears down nothing, so the teardown finalizer an earlier
		// read-write run added has nothing to remove it. This does, or a deleted declaration
		// stays Terminating.
		if err := reconcile.SetupReleaser(mgr, log); err != nil {
			return err
		}
	} else {
		reconciler, err = reconcile.SetupWithManager(mgr, cfg, log)
		if err != nil {
			return err
		}
		reconciler.Devices = deviceIndex
		reconciler.Capabilities = capabilities
	}

	tlsCfg, err := tlsConfig(cfg)
	if err != nil {
		return err
	}
	if cfg.TLSInsecureSkipVerify {
		log.Warn("hub certificate verification is disabled; anyone on the path can impersonate the hub " +
			"and declare workloads on this cluster. Use HANGAR_HUB_CA_FILE instead outside local development")
	}

	restCfg, err := ctrl.GetConfig()
	if err != nil {
		return fmt.Errorf("load kubeconfig: %w", err)
	}
	directClient, err := ctrlclient.New(restCfg, ctrlclient.Options{Scheme: mgr.GetScheme()})
	if err != nil {
		return fmt.Errorf("build checkpoint client: %w", err)
	}
	// Discovery is how the observation loop learns whether a leader-worker backend is installed.
	// Built here from the same rest config rather than inside the discoverer so that a cluster
	// this operator cannot even reach discovery on fails at startup, where it is visible, rather
	// than degrading to "no multi-node backend" on every tick.
	discoveryClient, err := discovery.NewDiscoveryClientForConfig(restCfg)
	if err != nil {
		return fmt.Errorf("build discovery client: %w", err)
	}
	cursor := checkpoint.NewCRDStore(directClient, cfg.Namespace)

	store := &reconcile.Store{
		Client:            mgr.GetClient(),
		Reader:            mgr.GetAPIReader(),
		Namespace:         cfg.Namespace,
		Log:               log,
		Checkpoint:        cursor,
		ReleaseFinalizers: cfg.ReadOnly,
	}
	observer := observe.New(&observe.Observer{
		ClusterID:       cfg.ClusterID,
		OperatorVersion: version,
		ReadOnly:        cfg.ReadOnly,
		Discoverer: &observe.KubeDiscoverer{
			Client: directClient,
			Groups: &observe.DiscoveryGroups{Client: discoveryClient},
			DCGM: &observe.DCGMSource{
				Endpoint: cfg.DCGMEndpoint,
				Client:   &http.Client{Timeout: cfg.DCGMTimeout},
				Kube:     directClient,
				Selector: cfg.DCGMSelector,
				Log:      log,
			},
			PoolLabelKey: cfg.PoolLabelKey,
			Log:          log,
		},
		Devices:        deviceIndex,
		Capabilities:   capabilities,
		Interval:       cfg.ObserveInterval,
		Timeout:        cfg.ObserveTimeout,
		ResyncInterval: cfg.ResyncInterval,
		Log:            log,
	})

	// Benchmarks are the one thing this operator does that needs a pod's log read back, and logs
	// are a subresource controller-runtime does not serve. A cluster whose credentials cannot
	// read them still runs everything else; only benchmarks refuse, and they say so.
	// Declared as the interface and left nil on failure. Assigning a typed nil pointer into it
	// would produce a non-nil interface holding nothing, and the executor's guard would wave it
	// through to a nil dereference instead of refusing.
	var logs operate.LogReader
	if reader, err := operate.NewPodLogs(restCfg); err != nil {
		log.Warn("no log client; benchmarks will refuse to run", "error", err)
	} else {
		logs = reader
	}

	executor := operate.New(&operate.Executor{
		Client:    directClient,
		ClusterID: cfg.ClusterID,
		Log:       log,
		ReadOnly:  cfg.ReadOnly,
		Allowed:   cfg.AllowedOperations,
		Render:    reconcile.RenderOptions(cfg),
		Benchmark: render.BenchmarkOptions{
			Endpoint:     cfg.InferenceEndpoint,
			ArtifactBase: cfg.BenchmarkArtifactBase,
			CPU:          cfg.BenchmarkCPU,
			Memory:       cfg.BenchmarkMemory,
			Images:       cfg.BenchmarkImages,
			TokenSecret:  cfg.BenchmarkTokenSecret,
		},
		Logs:             logs,
		BenchmarkTimeout: cfg.BenchmarkTimeout,
	})

	client := syncpkg.NewClient(syncpkg.Options{
		HubURL:            cfg.HubURL,
		ClusterID:         cfg.ClusterID,
		Token:             cfg.EnrolmentToken,
		OperatorVersion:   version,
		TLSConfig:         tlsCfg,
		HeartbeatInterval: cfg.HeartbeatInterval,
		LivenessTimeout:   cfg.LivenessTimeout,
		BackoffMin:        cfg.BackoffMin,
		BackoffMax:        cfg.BackoffMax,
		SendQueueSize:     cfg.SendQueueSize,
		Handler:           reconcile.WithReporter(operate.WithExecutor(observe.WithObserver(store, observer), executor), reconciler),
		Checkpointer:      checkpoint.Adapt(cursor),
		Logger:            log,
	})

	observer.Bind(client)
	executor.Bind(client)
	// Ties the benchmark lane to the process's own lifetime. Without it the lane still drains —
	// New starts it — but a SIGTERM would stop the process with steps still queued and nothing
	// said about them, leaving the plane waiting for points on a curve this operator will never
	// measure. Start makes the shutdown report them refused and retryable instead.
	executor.Start(ctx)
	if reconciler != nil {
		reconciler.Bind(client)
	}

	healthSrv := health.New(cfg.HealthAddr, log, map[string]health.Check{
		"hub": func() error {
			if !client.Connected() {
				return errors.New("no hub session")
			}
			return nil
		},
		// A connected operator that has stopped observing is worse than a disconnected one:
		// the plane goes on presenting its last projection as current, with nothing to
		// suggest the cluster stopped answering. Readiness is the only place that can notice.
		"observe": func() error {
			since, ever := observer.Since(time.Now())
			if !ever {
				return errors.New("no observation completed yet")
			}
			if since > cfg.ObserveStaleAfter {
				return fmt.Errorf("last observation was %s ago", since.Truncate(time.Second))
			}
			return nil
		},
	})

	// The hub session, the observation loop and the demand resync run inside the manager, so that
	// with leader election on they run only in the replica holding the lease, exactly like the
	// controller. Outside it every replica dialled the hub and streamed its own snapshots, and the
	// hub displaced one session with the other on every reconnect. A replica waiting for the lease
	// reports not ready, since it holds no hub session.
	leaderOnly := map[string]func(context.Context) error{
		"hub session": client.Run,
		"observation": observer.Run,
	}
	// Read-only has no reconciler and nothing it rendered to state. A snapshot from here would be
	// an empty claim of completeness over deployments somebody else owns.
	if reconciler != nil {
		leaderOnly["demand resync"] = func(ctx context.Context) error {
			return reconciler.RunDemandResync(ctx, cfg.ResyncInterval)
		}
	}
	for name, run := range leaderOnly {
		if err := mgr.Add(manager.RunnableFunc(run)); err != nil {
			return fmt.Errorf("register %s: %w", name, err)
		}
	}

	g, gctx := errgroup.WithContext(ctx)
	g.Go(func() error { return healthSrv.Run(gctx) })
	g.Go(func() error {
		if cfg.ReadOnly {
			log.Info("starting manager without the reconcile loop", "read_only", true)
		} else {
			log.Info("starting reconcile loop")
		}
		return mgr.Start(gctx)
	})

	log.Info("operator started", "hub", cfg.HubURL, "namespace", cfg.Namespace,
		"read_only", cfg.ReadOnly)
	err = g.Wait()
	if errors.Is(err, context.Canceled) {
		err = nil
	}
	log.Info("operator stopped")
	return err
}

func newLogger(cfg *config.Config) *slog.Logger {
	opts := &slog.HandlerOptions{Level: cfg.LogLevel}
	var h slog.Handler
	if cfg.LogFormat == "text" {
		h = slog.NewTextHandler(os.Stderr, opts)
	} else {
		h = slog.NewJSONHandler(os.Stderr, opts)
	}
	return slog.New(h)
}

func tlsConfig(cfg *config.Config) (*tls.Config, error) {
	t := &tls.Config{MinVersion: tls.VersionTLS12, InsecureSkipVerify: cfg.TLSInsecureSkipVerify} //nolint:gosec
	if cfg.TLSCAFile == "" {
		return t, nil
	}
	pem, err := os.ReadFile(cfg.TLSCAFile)
	if err != nil {
		return nil, fmt.Errorf("read hub CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf("hub CA file %s contains no certificates", cfg.TLSCAFile)
	}
	t.RootCAs = pool
	return t, nil
}
