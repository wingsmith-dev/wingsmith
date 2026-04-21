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
	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

func main() {
	listenAddr := flag.String("listen", ":8080", "HTTP listen address")
	planeURL := flag.String("plane-url", "", "hangar plane base URL (HANGAR_PLANE_URL env fallback)")
	resync := flag.Duration("resync", 30*time.Second, "how often to re-read the routing table")
	credentialsPath := flag.String("credentials", "", "path to the mounted credential file (HANGAR_GATEWAY_CREDENTIALS env fallback)")
	flag.Parse()

	if *planeURL == "" {
		*planeURL = os.Getenv("HANGAR_PLANE_URL")
	}
	if *credentialsPath == "" {
		*credentialsPath = os.Getenv("HANGAR_GATEWAY_CREDENTIALS")
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

	router := routing.New(
		plane.New(*planeURL, planeToken, 5*time.Second),
		func(upstream *url.URL, credentialID string) http.Handler {
			return gateway.NewReverseProxy(upstream, credentials.Headers(credentialID))
		},
	)

	// The first load must succeed. Coming up with an empty table would answer
	// every request with "model not found", which reads as an outage of the
	// models rather than as the misconfiguration it is.
	if err := router.Load(ctx); err != nil {
		slog.Error("gateway: could not load the routing table", "err", err)
		os.Exit(1)
	}
	go router.RunResync(ctx, *resync)

	srv := &http.Server{
		Addr: *listenAddr,
		Handler: gateway.New(gateway.Config{
			Router:  router,
			Callers: callers,
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
}
