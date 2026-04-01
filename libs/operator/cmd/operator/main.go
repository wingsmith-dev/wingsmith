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

	"golang.org/x/sync/errgroup"

	ctrl "sigs.k8s.io/controller-runtime"
	ctrlclient "sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/checkpoint"
	"github.com/hangar-dev/hangar/libs/operator/internal/config"
	"github.com/hangar-dev/hangar/libs/operator/internal/health"
	"github.com/hangar-dev/hangar/libs/operator/internal/observe"
	"github.com/hangar-dev/hangar/libs/operator/internal/operate"
	"github.com/hangar-dev/hangar/libs/operator/internal/reconcile"
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

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	mgr, err := reconcile.NewManager(cfg, log)
	if err != nil {
		return err
	}
	// The manager runs either way. Its cached client is what the desired-state store reads and
	// writes, and that store keeps working read-only — what stops is the controller that would
	// turn those CRDs into Kubernetes objects, which is simply never registered.
	if !cfg.ReadOnly {
		if err := reconcile.SetupWithManager(mgr, cfg, log); err != nil {
			return err
		}
	}

	tlsCfg, err := tlsConfig(cfg)
	if err != nil {
		return err
	}

	restCfg, err := ctrl.GetConfig()
	if err != nil {
		return fmt.Errorf("load kubeconfig: %w", err)
	}
	directClient, err := ctrlclient.New(restCfg, ctrlclient.Options{Scheme: mgr.GetScheme()})
	if err != nil {
		return fmt.Errorf("build checkpoint client: %w", err)
	}
	cursor := checkpoint.NewCRDStore(directClient, cfg.Namespace)

	store := &reconcile.Store{
		Client:     mgr.GetClient(),
		Namespace:  cfg.Namespace,
		Log:        log,
		Checkpoint: cursor,
	}
	observer := observe.New(&observe.Observer{
		ClusterID:    cfg.ClusterID,
		AgentVersion: version,
		Discoverer: &observe.KubeDiscoverer{
			Client: directClient,
			DCGM: &observe.DCGMSource{
				Endpoint: cfg.DCGMEndpoint,
				Client:   &http.Client{Timeout: cfg.DCGMTimeout},
				Log:      log,
			},
			Log: log,
		},
		Interval: cfg.ObserveInterval,
		Log:      log,
	})

	executor := operate.New(&operate.Executor{
		Client:    directClient,
		ClusterID: cfg.ClusterID,
		Log:       log,
		ReadOnly:  cfg.ReadOnly,
	})

	client := syncpkg.NewClient(syncpkg.Options{
		HubURL:            cfg.HubURL,
		ClusterID:         cfg.ClusterID,
		Token:             cfg.EnrolmentToken,
		AgentVersion:      version,
		TLSConfig:         tlsCfg,
		HeartbeatInterval: cfg.HeartbeatInterval,
		LivenessTimeout:   cfg.LivenessTimeout,
		BackoffMin:        cfg.BackoffMin,
		BackoffMax:        cfg.BackoffMax,
		SendQueueSize:     cfg.SendQueueSize,
		Handler:           operate.WithExecutor(observe.WithObserver(store, observer), executor),
		Checkpointer:      checkpoint.Adapt(cursor),
		Logger:            log,
	})

	observer.Bind(client)
	executor.Bind(client)

	healthSrv := health.New(cfg.HealthAddr, log, map[string]health.Check{
		"hub": func() error {
			if !client.Connected() {
				return errors.New("no hub session")
			}
			return nil
		},
	})

	g, gctx := errgroup.WithContext(ctx)
	g.Go(func() error { return healthSrv.Run(gctx) })
	g.Go(func() error { return client.Run(gctx) })
	g.Go(func() error { return observer.Run(gctx) })
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
