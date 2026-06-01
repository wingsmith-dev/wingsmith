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

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// Target is the address lookup the handler needs. The Resolver in this package
// satisfies it; a caller holding the address some other way can pass its own.
type Target interface {
	URL() (*url.URL, error)
}

// DeploymentTarget is a Target that can also answer for one deployment.
//
// Separate from Target, and optional, because the two are different amounts of
// knowledge and only one of them requires a cluster. The Resolver satisfies it;
// a caller that was handed a single address — the endpoint override, a test, an
// embedder — has one frontend by construction and nothing to choose between, so
// requiring this of every Target would be asking for an answer nobody but
// discovery can give.
type DeploymentTarget interface {
	Target
	URLFor(deploymentID string) (*url.URL, error)
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
// Every request that arrived over the tunnel lands here and is forwarded to the
// frontend serving the deployment the gateway already resolved it to. That is
// the only routing decision this hop makes, and it makes it from a header
// rather than from the body: the gateway is the hop that parsed the body, and
// re-deriving its answer here — by asking each frontend what models it serves —
// would be a second answer that can disagree with the first. Which replica
// behind the frontend takes the request is the serving runtime's decision and
// stays there.
//
// A request naming no deployment, or naming one this cluster renders no
// frontend for, goes to the shared frontend. That is the whole of why this can
// ship dark: every cluster runs one shared frontend and none renders a
// per-deployment one yet, so every request falls through to exactly the address
// it reached before.
func NewHandler(target Target) http.Handler {
	proxy := newReverseProxy()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstream, err := frontendFor(target, r.Header.Get(tunnel.HeaderDeploymentID))
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
			// The same status and a different code, because they are different
			// facts and only one of them is about the cluster. This one is one
			// deployment's frontend not being up while everything beside it
			// answers normally, which is the ordinary shape of the seconds
			// after a deployment is created — so it is worth retrying, and it
			// is worth being able to tell from a cluster that is serving
			// nothing at all when reading a log or a dashboard.
			if errors.Is(err, ErrNoDeploymentFrontend) {
				slog.Warn("frontend: no frontend for this deployment yet, refusing",
					"path", r.URL.Path, "err", err)
				writeError(w, http.StatusServiceUnavailable, "deployment_frontend_not_ready",
					"The frontend for this model is not up in this cluster yet.")
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

// frontendFor picks the frontend for one request.
//
// An empty id is not an error and never a refusal. It is what a gateway that
// could not name a deployment sends — an older build, an upstream the plane
// published without one — and the shared frontend is the right answer to it. A
// Target that cannot answer per deployment is in the same position and gets the
// same treatment, which is what keeps the endpoint override working: a cluster
// fronting its models with something Dynamo never labelled has one address and
// nothing to choose between.
func frontendFor(target Target, deploymentID string) (*url.URL, error) {
	if deploymentID == "" {
		return target.URL()
	}
	if byDeployment, ok := target.(DeploymentTarget); ok {
		return byDeployment.URLFor(deploymentID)
	}
	return target.URL()
}

// newReverseProxy forwards to whichever address the handler resolved.
func newReverseProxy() *httputil.ReverseProxy {
	return &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			// Stripped on the outbound leg and not on the inbound request, so
			// that the header the handler read is still there for anything
			// observing it. It goes no further than this hop either way: it is
			// ours, it has already been acted on, and Dynamo has no use for a
			// header naming an object it has never heard of.
			r.Out.Header.Del(tunnel.HeaderDeploymentID)

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
