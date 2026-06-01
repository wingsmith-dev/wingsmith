// Package serving is the gateway's HTTP entry point: one hop that turns a
// public model name into a request against the cluster currently serving it.
//
// It owns the mux, the reverse proxy, the caller guard, per-request observation
// and readiness. The model-to-cluster table it reads is routing's, and the wire
// into a cluster is libs/packages/tunnel's.
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
package serving

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
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
	// Metrics, when set, receives one observation per request. Nil means the
	// handler behaves exactly as it did before there was a /metrics endpoint,
	// which is what keeps every existing caller of New compiling and every
	// existing test honest.
	Metrics *metrics.Recorder
	// Probes, when set, recognises the conformance suite's own traffic so it can
	// be excluded from an availability ratio. Nil means every request is live.
	Probes *ProbeMarker
	// Ready, when set, gates `GET /readyz`. Nil means always ready, which is
	// what a gateway whose upstreams all have addresses is.
	Ready ReadinessChecker
}

// New builds the gateway handler.
//
// The probes are registered outside everything else on purpose: they are what
// Kubernetes calls and they must answer without the plane, a table, or a token.
//
// They are two probes and not one, because they answer different questions.
// `/healthz` is liveness and is unconditional — a replica that cannot serve must
// not be restarted for it, since restarting is exactly what will not fix a
// missing tunnel. `/readyz` is readiness and reports whether this replica can
// actually carry a request, which for a tunnelled cluster is a per-pod fact that
// no load balancer can see for itself.
func New(cfg Config) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		ready, detail := true, "ok"
		if cfg.Ready != nil {
			ready, detail = cfg.Ready.Ready()
		}
		if !ready {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte("not ready: " + detail))
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ready: " + detail))
	})

	var handler http.Handler = withRouting(cfg.Router)
	if cfg.Callers != nil {
		handler = withGuard(cfg.Callers, handler)
	} else {
		slog.Warn("gateway: no caller token configured, anything that can reach this port can use it")
	}
	mux.Handle("/", handler)

	return observed(cfg.Metrics, cfg.Probes, mux)
}

// modelEnvelope picks the routing key out of an otherwise untouched body.
type modelEnvelope struct {
	Model string          `json:"model"`
	Stop  json.RawMessage `json:"stop"`
}

// withRouting dispatches a request to the cluster serving its `model`.
//
// `GET /v1/models` is answered from the table, because the gateway — not any
// one cluster — is what knows the whole set.
func withRouting(resolver Resolver) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/v1/models" {
			markEndpoint(r, metrics.EndpointCatalog, "")
			writeModelList(w, resolver.Names())
			return
		}

		// Before the endpoint is known a request has no endpoint label, and it
		// is not given the model string it asked for: a free-text label is an
		// unbounded axis handed to whoever is calling.
		markEndpoint(r, metrics.EndpointUnknown, "")

		body, ok := readBody(w, r)
		if !ok {
			return
		}

		var envelope modelEnvelope
		if err := json.Unmarshal(body, &envelope); err != nil {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
			return
		}
		if envelope.Model == "" {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusBadRequest, "missing_model", "You must provide a model identifier.")
			return
		}
		if tooManyStopSequences(r.URL.Path, envelope.Stop) {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusBadRequest, "too_many_stop_sequences",
				"You may provide at most four stop sequences.")
			return
		}

		route, err := resolver.Resolve(envelope.Model)
		if err != nil {
			if errors.Is(err, routing.ErrUnknownEndpoint) {
				MarkOutcome(r, metrics.OutcomeClientError)
				writeError(w, http.StatusNotFound, "model_not_found",
					"The model '"+envelope.Model+"' is not served here.")
				return
			}
			MarkOutcome(r, metrics.OutcomeInternalError)
			slog.Error("gateway: resolution failed", "model", envelope.Model, "err", err)
			writeError(w, http.StatusInternalServerError, "routing_error", "Unable to route this request.")
			return
		}
		markEndpoint(r, route.ID, route.Name)

		// The endpoint's own terms, before an upstream is chosen. A policy is a
		// property of the name that was sold, so every upstream behind it must
		// answer a request the same way — checking after Pick would let the same
		// call be a 400 or a 200 depending on which cluster took it.
		if code, message, ok := checkPolicy(route.Policy, body); !ok {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusBadRequest, code, message)
			return
		}

		upstream, err := route.Pick()
		if err != nil {
			MarkOutcome(r, metrics.OutcomeNoUpstream)
			slog.Error("gateway: no upstream for endpoint", "model", envelope.Model, "err", err)
			writeError(w, http.StatusServiceUnavailable, "no_capacity",
				"The model '"+envelope.Model+"' is temporarily unavailable.")
			return
		}

		// The caller named an endpoint; the engine answers to something else.
		// Rewritten after Pick because the served name belongs to the upstream
		// and not to the endpoint — one endpoint can front two clusters running
		// different recipes, and each wants its own name.
		if served := upstream.ServedModelName; served != "" && served != envelope.Model {
			rewritten, err := withModel(body, served)
			if err != nil {
				MarkOutcome(r, metrics.OutcomeInternalError)
				slog.Error("gateway: could not rewrite the model name",
					"model", envelope.Model, "served", served, "err", err)
				writeError(w, http.StatusInternalServerError, "routing_error", "Unable to route this request.")
				return
			}
			body = rewritten
		}

		// The deployment this request was resolved to, forwarded so the agent
		// can hand it to that model's own frontend instead of asking each
		// frontend what it serves. The gateway is the only hop that has parsed
		// the body, so it is the only hop that can name it.
		//
		// Written unconditionally, and deleted just as unconditionally when
		// there is nothing to write. That is a security requirement and not
		// tidiness: httputil.ReverseProxy clones the inbound headers verbatim
		// onto the outbound request, so a value a caller sent under this name
		// would ride all the way into the cluster and choose which model's
		// frontend answers them — a caller picking somebody else's deployment,
		// across tenants, using a header they made up. The Set covers the case
		// where the gateway has an answer and the Del covers the case where it
		// does not, and between them the caller's own value never survives this
		// line.
		//
		// Not the per-credential header map on the pooled proxy, which is where
		// every other stamped header lives: that map is built once per handler,
		// and the deployment differs per request.
		if upstream.DeploymentID != "" {
			r.Header.Set(tunnel.HeaderDeploymentID, upstream.DeploymentID)
		} else {
			r.Header.Del(tunnel.HeaderDeploymentID)
		}

		// Replayable, using the buffer that was read to find the model rather
		// than a second copy of it. Without a GetBody, HTTP/2 refuses to retry
		// anything with a body after an agent sends GOAWAY — which is every
		// completion there is, and they are the requests it is cheapest to get
		// right because the agent never processed them.
		r = tunnel.Replayable(r, body)
		r.Header.Del("Content-Length")
		r.TransferEncoding = nil

		markUpstream(r, upstream.DeploymentID, upstream.ClusterName)
		release := inFlight(r)
		defer release()

		upstream.Handler().ServeHTTP(w, r)
	})
}

// withModel returns the body with `model` replaced by the name the engine
// answers to, leaving every other field exactly as the caller sent it.
//
// Decoded one level deep only. The fields that matter here are large and
// entirely opaque to this hop — `messages`, `tools`, `response_format` — so they
// stay as raw bytes and are copied through rather than parsed and rebuilt. That
// keeps the cost proportional to the number of top-level keys instead of to the
// size of the conversation, and it means a body using some parameter this
// gateway has never heard of survives the trip unaltered.
//
// Key order is not preserved, because JSON objects are unordered and no engine
// may depend on it.
func withModel(body []byte, model string) ([]byte, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		return nil, err
	}
	encoded, err := json.Marshal(model)
	if err != nil {
		return nil, err
	}
	fields["model"] = encoded
	return json.Marshal(fields)
}

// readBody buffers the request body under the size cap.
func readBody(w http.ResponseWriter, r *http.Request) ([]byte, bool) {
	if r.Body == nil {
		MarkOutcome(r, metrics.OutcomeClientError)
		writeError(w, http.StatusBadRequest, "missing_body", "Request body is required.")
		return nil, false
	}

	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxRequestBodyBytes))
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusRequestEntityTooLarge, "request_too_large",
				"Request body exceeds the maximum supported size.")
			return nil, false
		}
		MarkOutcome(r, metrics.OutcomeClientError)
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
		// Read the tail of every response for its usage object, which is where
		// the token counts come from. Nothing is buffered: the window is fixed
		// and it is filled after the bytes have gone out.
		ModifyResponse: modifyResponse,
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
			// A cluster that never answered, one that timed out, and a caller
			// that hung up are three different failures wearing one status
			// code, and only two of them are ours.
			markProxyError(r, err)
			slog.Error("gateway: upstream failed", "path", r.URL.Path, "err", err)
			writeError(w, http.StatusBadGateway, "upstream_unavailable",
				"The cluster serving this model did not respond.")
		},
	}
}

// statusRecorder captures the status code for the access log, and the moment
// the first byte reached the caller for the metrics.
//
// It is extended rather than wrapped in a second recorder on purpose. The
// Unwrap below is what keeps http.ResponseController reaching Flush, and
// FlushInterval: -1 on the proxy is what makes a stream arrive token by token
// instead of as one long pause — a wrapper that hides Flush destroys that
// silently and only under load.
type statusRecorder struct {
	http.ResponseWriter
	status int
	obs    *observation
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// Write stamps the first byte out. Headers arriving are not a token: an
// upstream sends those as soon as it accepts the request, and time to first
// token is what a caller actually waits for.
func (s *statusRecorder) Write(b []byte) (int, error) {
	if s.obs != nil && len(b) > 0 {
		s.obs.stampFirstByte(time.Now())
	}
	return s.ResponseWriter.Write(b)
}

// Unwrap exposes the underlying writer so http.ResponseController can still
// reach Flush, which streaming responses depend on.
func (s *statusRecorder) Unwrap() http.ResponseWriter { return s.ResponseWriter }

// observed is the outermost wrapper: it carries the per-request observation,
// writes the access log, and records the request once it is finished.
//
// One wrapper rather than two, for the reason stated on statusRecorder.
//
// The kubelet's probes are neither logged nor counted. A probe is not a request
// against anyone's availability, and counting a few thousand of them a day
// against an endpoint's failure budget would make the budget meaningless in the
// helpful direction.
func observed(recorder *metrics.Recorder, probes *ProbeMarker, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		obs := &observation{start: start, origin: probes.Origin(r), recorder: recorder}
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK, obs: obs}
		r = r.WithContext(context.WithValue(r.Context(), observationKey{}, obs))

		// Deferred, and the panic is recovered, because httputil.ReverseProxy
		// reports a failure halfway through copying a response body by panicking
		// http.ErrAbortHandler rather than through ErrorHandler. That panic
		// unwinds straight past a non-deferred observation, so every stream that
		// was truncated mid-generation — the exact failure a caller notices most
		// — was recorded as nothing at all.
		//
		// It is re-raised afterwards. The panic is how a handler tells net/http
		// to sever the response instead of finishing its framing, and swallowing
		// it would hand the caller a truncated stream with a clean ending on it.
		defer func() {
			aborted := recover()
			if aborted != nil {
				markAborted(r, aborted)
			}
			if !isProbePath(r.URL.Path) {
				elapsed := time.Since(start)
				slog.Info("gateway: request",
					"method", r.Method,
					"path", r.URL.Path,
					"status", rec.status,
					"duration_ms", elapsed.Milliseconds(),
					"aborted", aborted != nil,
				)
				if recorder != nil {
					recorder.Observe(obs.sample(rec.status, elapsed))
				}
			}
			if aborted != nil {
				panic(aborted)
			}
		}()

		next.ServeHTTP(rec, r)
	})
}

// isProbePath reports whether a path is one of the kubelet's.
func isProbePath(path string) bool {
	return path == "/healthz" || path == "/readyz"
}

// inFlight marks a request as being proxied for as long as it is, and returns
// the release. A request with no recorder behind it releases nothing.
func inFlight(r *http.Request) func() {
	obs := observationFrom(r.Context())
	if obs == nil || obs.recorder == nil {
		return func() {}
	}
	obs.mu.Lock()
	endpoint, name, origin := obs.endpoint, obs.endpointName, obs.origin
	deployment, cluster := obs.deployment, obs.cluster
	obs.mu.Unlock()
	if endpoint == "" {
		endpoint = metrics.EndpointUnknown
	}
	return obs.recorder.InFlight(endpoint, name, origin, deployment, cluster)
}
