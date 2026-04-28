package dynamo

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"
)

// Target is the address lookup the handler needs. The Resolver in this package
// satisfies it; a caller holding the address some other way can pass its own.
type Target interface {
	URL() (*url.URL, error)
}

// targetKey carries the address chosen for one request through to Rewrite.
//
// The address is read once per request and passed forward rather than read
// again inside Rewrite, because Rewrite cannot fail: a resolver that went empty
// between the two reads would leave the proxy with nowhere to send the request
// and no way to say so.
type targetKey struct{}

// idleTimeout, headerTimeout and the connection pool size are the local
// equivalents of the gateway's own upstream transport. The frontend is one
// ClusterIP a few milliseconds away, so the pool is generous and the header
// timeout is the only bound: generation legitimately runs for minutes at long
// contexts, and there is no round-trip deadline for that reason.
const (
	idleConnsPerHost = 64
	idleTimeout      = 90 * time.Second
	headerTimeout    = 5 * time.Minute
)

// NewHandler builds the agent's local proxy.
//
// Every request that arrived over the tunnel lands here and is forwarded
// unchanged: this hop routes on nothing, because the gateway already decided
// which cluster the request belongs to and the serving runtime decides which
// replica inside it.
func NewHandler(target Target) http.Handler {
	proxy := newReverseProxy()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstream, err := target.URL()
		if err != nil {
			// Not a 502: nothing upstream failed, there is nothing upstream
			// yet. A cluster whose graphs are still coming up is unavailable
			// and retryable, and a caller can tell those apart by status.
			if errors.Is(err, ErrNoFrontend) {
				slog.Warn("frontend: no address yet, refusing", "path", r.URL.Path)
				writeError(w, http.StatusServiceUnavailable, "no_frontend",
					"This cluster is not serving inference traffic yet.")
				return
			}
			slog.Error("frontend: could not resolve an address", "path", r.URL.Path, "err", err)
			writeError(w, http.StatusInternalServerError, "frontend_error",
				"This cluster could not route the request internally.")
			return
		}

		r = r.WithContext(context.WithValue(r.Context(), targetKey{}, upstream))
		proxy.ServeHTTP(w, r)
	})
}

// newReverseProxy forwards to whichever address the handler resolved.
func newReverseProxy() *httputil.ReverseProxy {
	return &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			upstream, _ := r.In.Context().Value(targetKey{}).(*url.URL)
			if upstream == nil {
				return
			}
			r.SetURL(upstream)
			r.Out.Host = upstream.Host
			r.SetXForwarded()
		},
		// Flush every write. Completions with "stream": true are SSE, and the
		// default heuristic buffers a body with no known length — which turns a
		// token-by-token stream into one long pause and then the whole answer.
		// The tunnel on the other side of this handler cannot un-buffer it.
		FlushInterval: -1,
		Transport: &http.Transport{
			MaxIdleConnsPerHost:   idleConnsPerHost,
			IdleConnTimeout:       idleTimeout,
			ResponseHeaderTimeout: headerTimeout,
			ExpectContinueTimeout: time.Second,
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			// A caller that hung up is not a cluster failure, and it has
			// nobody left to tell: writing a status here would only be a write
			// to a closed connection.
			if errors.Is(err, context.Canceled) || errors.Is(r.Context().Err(), context.Canceled) {
				return
			}
			slog.Error("frontend: the inference frontend failed", "path", r.URL.Path, "err", err)
			writeError(w, http.StatusBadGateway, "frontend_unavailable",
				"The inference frontend in this cluster did not respond.")
		},
	}
}

// errorEnvelope is the OpenAI error shape, because everything downstream of
// this hop parses errors from an OpenAI-compatible API and a failure inside the
// cluster has to look like one too.
type errorEnvelope struct {
	Error errorBody `json:"error"`
}

type errorBody struct {
	Message string `json:"message"`
	Type    string `json:"type"`
	Code    string `json:"code"`
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(errorEnvelope{
		Error: errorBody{Message: message, Type: "invalid_request_error", Code: code},
	})
}
