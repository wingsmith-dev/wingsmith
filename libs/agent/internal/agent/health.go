package agent

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"time"
)

// ProbeConfig configures the probe listener.
type ProbeConfig struct {
	// Addr is where to listen.
	Addr string
	// Ready reports whether this agent can carry a request. Nil means always ready.
	Ready func() error
	// Coverage, when set, is reported on /readyz so a human reading a failing probe sees which
	// gateway replicas are missing, not just that some are.
	Coverage func() map[string]int
	// Expected, when set, is how many replicas the gateway says it is running. It is printed
	// alongside the coverage because the two together are the answer: this agent is not told the
	// replicas up front, so a short list is only a gap if this number is bigger than it.
	Expected func() int
	// Metrics, when set, is served at /metrics.
	Metrics *Metrics
}

// ProbeServer serves liveness, readiness and the scrape endpoint.
//
// All three on one listener, unlike the gateway, which keeps its scrape endpoint off the port that
// answers callers. There is no equivalent risk here: the agent's data path is a connection it
// dialled outwards and has no listener at all, so this port is already the only thing reachable and
// exists only for the kubelet and the scrape.
type ProbeServer struct {
	srv *http.Server
}

// NewProbeServer builds the listener.
func NewProbeServer(cfg ProbeConfig) *ProbeServer {
	return &ProbeServer{srv: &http.Server{
		Addr:              cfg.Addr,
		Handler:           ProbeHandler(cfg),
		ReadHeaderTimeout: 5 * time.Second,
	}}
}

// ProbeHandler is the probe mux on its own, so it can be served anywhere.
func ProbeHandler(cfg ProbeConfig) http.Handler {
	mux := http.NewServeMux()

	// Liveness is unconditional. An agent that cannot reach the gateway is not broken: the
	// gateway may be mid-rollout, or the network between them may be having a bad minute.
	// Restarting it would throw away the tunnels it does have. Readiness carries that instead.
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})

	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		var err error
		if cfg.Ready != nil {
			err = cfg.Ready()
		}
		status := http.StatusOK
		if err != nil {
			status = http.StatusServiceUnavailable
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(status)
		if err != nil {
			_, _ = fmt.Fprintf(w, "not ready: %s\n", err)
		} else {
			_, _ = w.Write([]byte("ok\n"))
		}
		if cfg.Coverage == nil {
			return
		}
		coverage := cfg.Coverage()
		if cfg.Expected != nil {
			_, _ = fmt.Fprintf(w, "gateway replicas: %d reached of %d\n", len(coverage), cfg.Expected())
		}
		gateways := make([]string, 0, len(coverage))
		for gateway := range coverage {
			gateways = append(gateways, gateway)
		}
		sort.Strings(gateways)
		for _, gateway := range gateways {
			_, _ = fmt.Fprintf(w, "%s: %d\n", gateway, coverage[gateway])
		}
	})

	if cfg.Metrics != nil {
		mux.Handle("GET "+metricsRegistryPath, cfg.Metrics.Handler())
	}
	return mux
}

// Run serves until ctx is cancelled, then shuts down.
func (p *ProbeServer) Run(ctx context.Context) error {
	errCh := make(chan error, 1)
	go func() {
		err := p.srv.ListenAndServe()
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		errCh <- err
	}()
	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		return p.srv.Shutdown(shutdown)
	}
}
