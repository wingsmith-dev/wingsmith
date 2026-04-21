// Command gateway is hangar's data plane: it accepts OpenAI-compatible requests
// and forwards each to a cluster currently serving the model it names.
package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/gateway"
	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

func main() {
	listenAddr := flag.String("listen", ":8080", "HTTP listen address")
	planeURL := flag.String("plane-url", "", "hangar plane base URL (HANGAR_PLANE_URL env fallback)")
	resync := flag.Duration("resync", 30*time.Second, "how often to re-read the routing table")
	credentialsPath := flag.String("credentials", "", "path to the mounted credential file (HANGAR_GATEWAY_CREDENTIALS env fallback)")
	// Both of the settings below default to something that works and are read
	// from the environment as well as the command line, and neither is ever
	// fatal by its absence. That is not politeness, it is the deploy path:
	// `just deploy plane` runs `kubectl apply -k infra/`, and infra/ carries
	// this component's manifest — so a manifest that names a flag the running
	// image does not know reaches the old binary first and flag.Parse exits 2,
	// which is a crashloop of the data plane caused by deploying the control
	// plane. An unknown environment variable is inert; an unknown flag is not.
	metricsAddr := flag.String("metrics-listen", "", "address for the Prometheus /metrics listener, empty for the default (HANGAR_GATEWAY_METRICS_LISTEN env fallback, \"off\" to disable)")
	probeToken := flag.String("probe-token", "", "token the conformance runner presents to mark its traffic as probe rather than live; accepts a comma-separated pair while rotating (HANGAR_GATEWAY_PROBE_TOKEN env fallback)")
	flag.Parse()

	if *planeURL == "" {
		*planeURL = os.Getenv("HANGAR_PLANE_URL")
	}
	if *credentialsPath == "" {
		*credentialsPath = os.Getenv("HANGAR_GATEWAY_CREDENTIALS")
	}
	if *metricsAddr == "" {
		*metricsAddr = os.Getenv("HANGAR_GATEWAY_METRICS_LISTEN")
	}
	if *metricsAddr == "" {
		*metricsAddr = defaultMetricsAddr
	}
	if *probeToken == "" {
		*probeToken = os.Getenv("HANGAR_GATEWAY_PROBE_TOKEN")
	}
	planeToken := os.Getenv("HANGAR_OPERATOR_TOKEN")
	callerToken := os.Getenv("HANGAR_GATEWAY_TOKEN")

	if *planeURL == "" || planeToken == "" {
		slog.Error("gateway: -plane-url and HANGAR_OPERATOR_TOKEN are both required")
		os.Exit(1)
	}

	credentials, err := gateway.LoadCredentials(*credentialsPath)
	if err != nil {
		slog.Error("gateway: could not read credentials", "err", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// Assigned through a nil check rather than directly: a (*TokenGuard)(nil) stored in a Guard
	// interface is not a nil interface, so a gateway with no token configured would reject every
	// caller instead of trusting its network.
	var callers gateway.Guard
	if guard := gateway.NewTokenGuard(callerToken); guard != nil {
		callers = guard
	}

	recorder := metrics.New()

	router := routing.New(
		plane.New(*planeURL, planeToken, 5*time.Second),
		func(upstream *url.URL, credentialID string) http.Handler {
			return gateway.NewReverseProxy(upstream, credentials.Headers(credentialID))
		},
		routing.WithObserver(recorder),
	)

	// The first load must succeed. Coming up with an empty table would answer
	// every request with "model not found", which reads as an outage of the
	// models rather than as the misconfiguration it is.
	if err := router.Load(ctx); err != nil {
		slog.Error("gateway: could not load the routing table", "err", err)
		os.Exit(1)
	}
	go router.RunResync(ctx, *resync)

	// A second listener, on its own port. The request path answers callers and
	// nothing else: a scrape endpoint on the same mux is one loose NetworkPolicy
	// away from being reachable by anyone who can reach the API, and it is the
	// one surface that has to stay answerable when the token guard, the plane
	// and the table are all unavailable.
	metricsSrv := serveMetrics(*metricsAddr, recorder)

	srv := &http.Server{
		Addr: *listenAddr,
		Handler: gateway.New(gateway.Config{
			Router:  router,
			Callers: callers,
			Metrics: recorder,
			Probes:  gateway.NewProbeMarker(*probeToken),
		}),
		// No WriteTimeout: it is absolute rather than idle-based, so any value
		// would sever long generations and SSE streams mid-token.
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	go func() {
		slog.Info("gateway: listening", "addr", *listenAddr, "endpoints", len(router.Names()), "revision", router.Revision())
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("gateway: serve failed", "err", err)
			stop()
		}
	}()

	<-ctx.Done()
	slog.Info("gateway: shutting down")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Error("gateway: shutdown failed", "err", err)
	}
	if metricsSrv != nil {
		if err := metricsSrv.Shutdown(shutdownCtx); err != nil {
			slog.Error("gateway: metrics shutdown failed", "err", err)
		}
	}
}

// defaultMetricsAddr is where the scrape looks when nothing says otherwise, so
// that a gateway rolled out with no manifest change is already exporting.
const defaultMetricsAddr = ":9090"

// serveMetrics starts the exposition listener.
//
// Every failure in here is logged and survived. Losing the ability to describe
// the data plane is a monitoring incident; refusing to run the data plane
// because it cannot be described would be an outage, and the numbers this
// listener exists to publish are the ones that would have to report it.
func serveMetrics(addr string, recorder *metrics.Recorder) *http.Server {
	if addr == "" || addr == "off" {
		slog.Warn("gateway: metrics listener disabled, this gateway reports no failure rate")
		return nil
	}

	mux := http.NewServeMux()
	mux.Handle("GET /metrics", recorder.Handler())
	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		slog.Info("gateway: metrics listening", "addr", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("gateway: metrics listener stopped, serving traffic without it", "err", err)
		}
	}()
	return srv
}
