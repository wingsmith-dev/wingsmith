// Package health serves the liveness and readiness endpoints.
package health

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"time"
)

// Check reports whether a subsystem is ready. It must not block.
type Check func() error

// Server exposes /healthz and /readyz.
//
// Liveness is unconditional: a partitioned operator is still healthy, because the
// outbound-agent architecture exists so that the cluster keeps working when the
// control plane is unreachable. Readiness carries the stream state instead.
type Server struct {
	srv    *http.Server
	log    *slog.Logger
	checks map[string]Check
}

// New builds a health server bound to addr.
func New(addr string, log *slog.Logger, checks map[string]Check) *Server {
	s := &Server{log: log.With("component", "health"), checks: checks}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("GET /readyz", s.ready)
	s.srv = &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	return s
}

func (s *Server) ready(w http.ResponseWriter, _ *http.Request) {
	for name, check := range s.checks {
		if err := check(); err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(name + ": " + err.Error() + "\n"))
			return
		}
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

// Run serves until ctx is cancelled, then shuts down gracefully.
func (s *Server) Run(ctx context.Context) error {
	errCh := make(chan error, 1)
	go func() {
		err := s.srv.ListenAndServe()
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
		return s.srv.Shutdown(shutdown)
	}
}
