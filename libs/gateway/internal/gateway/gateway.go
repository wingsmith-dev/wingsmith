// Package gateway is hangar's data plane: one hop that turns a public model
// name into a request against the cluster currently serving it.
//
// It is deliberately thin. It does not authenticate callers, meter tokens, or
// bill anyone — whoever puts a gateway in front of their users already has a
// place where that happens, and doing it twice is how two systems end up
// disagreeing about what a customer owes. What it does own is the one thing
// only hangar knows: which clusters are serving a model right now, and how to
// reach them.
//
// Replica selection inside a cluster is not this hop's business either. The
// serving runtime routes on live queue depth and KV-cache residency, which is
// state that does not leave the cluster and should not.
package gateway

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

// maxRequestBodyBytes caps how much of a body is buffered to read its `model`.
// Routing has to see the body, so this is the one place the gateway is not a
// pure stream. A long-context prompt is legitimately large, not unbounded.
const maxRequestBodyBytes = 32 << 20

// Resolver is the routing lookup this handler needs.
type Resolver interface {
	Resolve(name string) (routing.Route, error)
	Names() []string
	Revision() string
}

// Config holds the handler's settings.
type Config struct {
	// Router resolves a request's `model` to an upstream.
	Router Resolver
	// Callers, when set, is the shared secret every request must present. Empty
	// means the gateway trusts its network, which is the in-cluster case: a
	// ClusterIP service with no ingress in front of it.
	Callers Guard
}

// New builds the gateway handler.
//
// `/healthz` is registered outside everything else on purpose: it is what
// Kubernetes probes and it must answer without the plane, a table, or a token.
func New(cfg Config) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	var handler http.Handler = withRouting(cfg.Router)
	if cfg.Callers != nil {
		handler = withGuard(cfg.Callers, handler)
	} else {
		slog.Warn("gateway: no caller token configured, anything that can reach this port can use it")
	}
	mux.Handle("/", handler)

	return logging(mux)
}

// modelEnvelope picks the routing key out of an otherwise untouched body.
type modelEnvelope struct {
	Model string `json:"model"`
}

// withRouting dispatches a request to the cluster serving its `model`.
//
// `GET /v1/models` is answered from the table, because the gateway — not any
// one cluster — is what knows the whole set.
func withRouting(resolver Resolver) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/v1/models" {
			writeModelList(w, resolver.Names())
			return
		}

		body, ok := readBody(w, r)
		if !ok {
			return
		}

		var envelope modelEnvelope
		if err := json.Unmarshal(body, &envelope); err != nil {
			writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
			return
		}
		if envelope.Model == "" {
			writeError(w, http.StatusBadRequest, "missing_model", "You must provide a model identifier.")
			return
		}

		route, err := resolver.Resolve(envelope.Model)
		if err != nil {
			if errors.Is(err, routing.ErrUnknownEndpoint) {
				writeError(w, http.StatusNotFound, "model_not_found",
					"The model '"+envelope.Model+"' is not served here.")
				return
			}
			slog.Error("gateway: resolution failed", "model", envelope.Model, "err", err)
			writeError(w, http.StatusInternalServerError, "routing_error", "Unable to route this request.")
			return
		}

		upstream, err := route.Pick()
		if err != nil {
			slog.Error("gateway: no upstream for endpoint", "model", envelope.Model, "err", err)
			writeError(w, http.StatusServiceUnavailable, "no_capacity",
				"The model '"+envelope.Model+"' is temporarily unavailable.")
			return
		}

		r.Body = io.NopCloser(bytes.NewReader(body))
		r.ContentLength = int64(len(body))
		r.Header.Del("Content-Length")
		r.TransferEncoding = nil

		upstream.Handler().ServeHTTP(w, r)
	})
}

// readBody buffers the request body under the size cap.
func readBody(w http.ResponseWriter, r *http.Request) ([]byte, bool) {
	if r.Body == nil {
		writeError(w, http.StatusBadRequest, "missing_body", "Request body is required.")
		return nil, false
	}

	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxRequestBodyBytes))
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			writeError(w, http.StatusRequestEntityTooLarge, "request_too_large",
				"Request body exceeds the maximum supported size.")
			return nil, false
		}
		writeError(w, http.StatusBadRequest, "unreadable_body", "Could not read the request body.")
		return nil, false
	}
	return body, true
}

// NewReverseProxy forwards to one serving address, stamping the credential the
// address sits behind onto every outbound request.
//
// Headers are applied on the outbound leg only and overwrite whatever arrived
// under the same names: a caller must not be able to influence the credential
// this hop presents.
func NewReverseProxy(upstream *url.URL, headers http.Header) *httputil.ReverseProxy {
	return &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(upstream)
			r.Out.Host = upstream.Host
			r.SetXForwarded()
			for name, values := range headers {
				r.Out.Header[name] = values
			}
		},
		// Flush every write. Completions with "stream": true are SSE, and the
		// default heuristic buffers a body with no known length — which turns a
		// token-by-token stream into one long pause and then the whole answer.
		FlushInterval: -1,
		// No round-trip timeout. Generation legitimately runs for minutes at
		// long contexts, and a client that asked for 4k tokens is not hung.
		Transport: &http.Transport{
			Proxy:                 http.ProxyFromEnvironment,
			MaxIdleConnsPerHost:   64,
			IdleConnTimeout:       90 * time.Second,
			ResponseHeaderTimeout: 5 * time.Minute,
			ExpectContinueTimeout: time.Second,
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			slog.Error("gateway: upstream failed", "path", r.URL.Path, "err", err)
			writeError(w, http.StatusBadGateway, "upstream_unavailable",
				"The cluster serving this model did not respond.")
		},
	}
}

// statusRecorder captures the status code for the access log.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// Unwrap exposes the underlying writer so http.ResponseController can still
// reach Flush, which streaming responses depend on.
func (s *statusRecorder) Unwrap() http.ResponseWriter { return s.ResponseWriter }

func logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		if r.URL.Path == "/healthz" {
			return
		}
		slog.Info("gateway: request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rec.status,
			"duration_ms", time.Since(start).Milliseconds(),
		)
	})
}
