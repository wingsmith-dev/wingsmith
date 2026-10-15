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
	"os"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// DefaultMaxBodyBytes caps how much of a body is buffered to read its `model`
// when Config.MaxBodyBytes is zero. Routing has to see the body, so this is the
// one place the gateway is not a pure stream. A long-context prompt is
// legitimately large, not unbounded.
//
// It also caps an upload. At 100 MiB an hour of audio (~50 MiB) fits. The
// ceiling is memory, not policy: the body stays resident for the whole
// upstream call so the request remains replayable, one copy per request in
// flight (`tunnel.Replayable` reuses this buffer rather than taking a second),
// and a rewrite of the body takes another for as long as the request runs.
// BodyBudget bounds the total across requests.
//
// The answer to an upload larger than this is still a presigned upload and a
// job, not a bigger buffer on a synchronous hop.
const DefaultMaxBodyBytes = 100 << 20

// Resolver is the routing lookup this handler needs.
type Resolver interface {
	Resolve(name string) (routing.Route, error)
	Names() []string
	// NamesForTenant lists a public-zone caller's own catalog (docs/decisions/0016).
	NamesForTenant(tenantID string) []string
	Revision() string
}

// Config holds the handler's settings.
type Config struct {
	// Router resolves a request's `model` to an upstream.
	Router Resolver
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
	// Tenants is the host-to-tenant, hash-to-tenant and id-to-tenant lookup
	// (docs/decisions/0016). Required on the public zone. Nil on the internal
	// zone skips the disabled-tenant check, which only tests should do.
	Tenants Tenants
	// Zone says which listener this handler serves. Required: there is no
	// default, so a listener that forgets to say cannot end up unauthenticated.
	Zone Zone
	// LogUpstreamBodies writes a discarded upstream error body to the log
	// whole. Off by default and off in production: an engine's error can quote
	// the request it refused, and a caller's content does not belong in a log.
	// See logUpstreamError.
	LogUpstreamBodies bool
	// Limiter bounds each tenant's requests in flight and rate. Nil enforces
	// nothing. Share one across every listener on a replica.
	Limiter *Limiter
	// AllowUnauthenticatedPublic serves a tenant declaring `auth: none` on the
	// public zone. Off by default: a public listener that takes requests
	// without a key is a choice an operator makes on purpose, not one a
	// tenant row makes for them.
	AllowUnauthenticatedPublic bool
	// MaxBodyBytes caps one request body. Zero takes DefaultMaxBodyBytes.
	MaxBodyBytes int64
	// BodyReadTimeout bounds how long a caller may take to send its body once
	// the headers are in. Zero is no bound.
	BodyReadTimeout time.Duration
	// WriteIdleTimeout bounds how long one write to the caller may block, so a
	// caller that stops reading cannot hold a stream, a tunnel window and a
	// buffered body forever. It is per write and not per response: a long
	// generation that keeps arriving is never cut. Zero is no bound.
	WriteIdleTimeout time.Duration
	// BodyBudget bounds the request bytes buffered across every request at
	// once. Nil is no bound. Share one across every listener on a replica.
	BodyBudget *BodyBudget
	// MediaURLs restricts which remote media a request may ask an engine to
	// fetch. Nil allows every URL.
	MediaURLs *MediaURLPolicy
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
	if !cfg.Zone.valid() {
		panic("serving: Config.Zone must be ZoneInternal or ZonePublic")
	}
	if cfg.Zone == ZonePublic && cfg.Tenants == nil {
		panic("serving: the public zone needs a tenant table")
	}
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

	// Trust comes from the zone alone now (docs/decisions/0016): the internal zone is
	// the network boundary that used to be a shared token, and the public zone
	// is the tenant and key stages inside withRouting. Neither is a mux-level
	// wrapper any more, which is what a shared secret in front of every zone
	// alike could never have been.
	var routed http.Handler = withRouting(cfg)
	if cfg.LogUpstreamBodies {
		routed = withUpstreamBodiesLogged(routed)
	}
	mux.Handle("/", routed)

	return observed(cfg.Metrics, cfg.Probes, cfg.WriteIdleTimeout, mux)
}

// modelEnvelope picks the routing key out of an otherwise untouched body.
//
// `Stop` is JSON-only and stays empty for a multipart request, which costs
// nothing: the check it feeds is gated on the completion paths, and none of
// them are multipart.
//
// `NvExt` is held only to know that one was sent. What is inside it is read by
// withoutSteeringHints, exactly as the runtime reads it, and not from this copy.
type modelEnvelope struct {
	Model string          `json:"model"`
	Stop  json.RawMessage `json:"stop"`
	NvExt json.RawMessage `json:"nvext"`
}

// withRouting dispatches a request to the cluster serving its `model`, by
// running it through routingStages (pipeline.go) in order. Split out because
// this is the hot path for all inference traffic: each stage is a step
// somebody can read, test and insert beside on its own, rather than a spot in
// one function that keeps growing.
//
// `GET /v1/models` is answered from the table, because the gateway — not any
// one cluster — is what knows the whole set. That is `stageCatalog`, ahead of
// it the tenant stages that scope it (tenant_stages.go).
func withRouting(cfg Config) http.Handler {
	stages := routingStages(cfg)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		runPipeline(stages, w, r)
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

// readBody buffers the request body under the size cap, the read deadline and
// the shared buffer budget. The bytes it charged to the budget are released by
// the returned func, which the caller runs once the request is finished.
func readBody(w http.ResponseWriter, r *http.Request, limits bodyLimits) ([]byte, func(), bool) {
	if r.Body == nil {
		MarkOutcome(r, metrics.OutcomeClientError)
		writeError(w, http.StatusBadRequest, "missing_body", "Request body is required.")
		return nil, nil, false
	}

	maxBytes := limits.maxBytes
	if maxBytes <= 0 {
		maxBytes = DefaultMaxBodyBytes
	}
	if r.ContentLength > maxBytes {
		MarkOutcome(r, metrics.OutcomeClientError)
		writeError(w, http.StatusRequestEntityTooLarge, "request_too_large",
			"Request body exceeds the maximum supported size.")
		return nil, nil, false
	}

	// A deadline on the body alone, cleared once it is read. http.Server's own
	// ReadTimeout would also be the deadline of the background read that
	// watches for the caller hanging up during a stream, and expiring it there
	// cancels a generation that is still being read.
	if limits.readTimeout > 0 {
		rc := http.NewResponseController(w)
		if err := rc.SetReadDeadline(time.Now().Add(limits.readTimeout)); err == nil {
			defer func() { _ = rc.SetReadDeadline(time.Time{}) }()
		}
	}

	charged := &budgetReader{budget: limits.budget, reader: http.MaxBytesReader(w, r.Body, maxBytes)}
	body, err := io.ReadAll(charged)
	if err != nil {
		charged.release()
		// What is left of the body is still on the wire. Without this the
		// server would try to drain it after the answer, which for a caller
		// that has stopped sending never finishes.
		w.Header().Set("Connection", "close")
		var maxErr *http.MaxBytesError
		switch {
		case errors.As(err, &maxErr):
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusRequestEntityTooLarge, "request_too_large",
				"Request body exceeds the maximum supported size.")
		case errors.Is(err, errBudgetSpent):
			MarkOutcome(r, metrics.OutcomeShed)
			writeError(w, http.StatusServiceUnavailable, "no_capacity",
				"The gateway is buffering too many requests. Retry shortly.")
		case errors.Is(err, os.ErrDeadlineExceeded):
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusRequestTimeout, "request_timeout",
				"The request body was not received in time.")
		default:
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusBadRequest, "unreadable_body", "Could not read the request body.")
		}
		return nil, nil, false
	}
	return body, charged.release, true
}

// callerCredentialHeaders are what a caller presents to this hop and nothing
// past it may see. The tenant's API key, a session cookie and the conformance
// runner's probe token are all redeemed here; forwarding them would hand every
// serving cluster, agent and engine log a copy of a credential that is not
// theirs. Removed from the outbound request before the address's own
// credential is written, so a credential header this hop stamps under one of
// these names is still sent.
var callerCredentialHeaders = []string{
	"Authorization",
	"Proxy-Authorization",
	"Cookie",
	"X-Api-Key",
	"X-Hangar-Probe-Token",
	"X-Hangar-Origin",
	cacheScopeHeader,
}

// NewReverseProxy forwards to one serving address, stamping the credential the
// address sits behind onto every outbound request.
//
// The caller's own credentials are removed first (callerCredentialHeaders).
// Headers are then applied on the outbound leg only and overwrite whatever
// arrived under the same names: a caller must not be able to influence the
// credential this hop presents.
//
// headerTimeout bounds how long the upstream may hold the request without
// answering; zero takes tunnel.DefaultResponseHeaderTimeout, which is the same
// number every other hop on the inference path derives from.
func NewReverseProxy(upstream *url.URL, headers http.Header, headerTimeout time.Duration) *httputil.ReverseProxy {
	if headerTimeout <= 0 {
		headerTimeout = tunnel.DefaultResponseHeaderTimeout
	}
	return &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(upstream)
			r.Out.Host = upstream.Host
			r.SetXForwarded()
			for _, name := range callerCredentialHeaders {
				r.Out.Header.Del(name)
			}
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
			ResponseHeaderTimeout: headerTimeout,
			ExpectContinueTimeout: time.Second,
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			// A cluster that never answered, one that timed out, and a caller
			// that hung up are three different failures wearing one status
			// code, and only two of them are ours.
			markProxyError(r, err)
			slog.Error("gateway: upstream failed",
				"request_id", r.Header.Get(headerRequestID), "path", r.URL.Path, "err", err)
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

	// writeIdle, when set, is how long each write may block; rc arms it.
	writeIdle time.Duration
	rc        *http.ResponseController
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.armWrite()
	s.ResponseWriter.WriteHeader(code)
}

// armWrite moves the write deadline to writeIdle from now, before each write,
// so it bounds one blocked write and never the length of a stream.
func (s *statusRecorder) armWrite() {
	if s.rc != nil {
		_ = s.rc.SetWriteDeadline(time.Now().Add(s.writeIdle))
	}
}

// Write stamps the first byte out. Headers arriving are not a token: an
// upstream sends those as soon as it accepts the request, and time to first
// token is what a caller actually waits for.
func (s *statusRecorder) Write(b []byte) (int, error) {
	if s.obs != nil && len(b) > 0 {
		s.obs.stampFirstByte(time.Now())
	}
	s.armWrite()
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
func observed(recorder *metrics.Recorder, probes *ProbeMarker, writeIdle time.Duration, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		obs := &observation{start: start, origin: probes.Origin(r), recorder: recorder}
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK, obs: obs, writeIdle: writeIdle}
		if writeIdle > 0 {
			rec.rc = http.NewResponseController(w)
			// A write deadline outlives the response on a kept-alive
			// connection, and the next response would start already expired.
			defer func() { _ = rec.rc.SetWriteDeadline(time.Time{}) }()
		}
		r = r.WithContext(context.WithValue(r.Context(), observationKey{}, obs))
		r, ident := newIdentity(r)

		// Set on the inbound request so the proxy forwards it, and on the response before anything
		// else writes, so an error this hop writes itself carries it as well as a proxied answer.
		// The upstream's own copy is dropped by filterResponseHeaders and never doubles it.
		requestID := requestIDFor(r)
		r.Header.Set(headerRequestID, requestID)
		w.Header().Set(headerRequestID, requestID)

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

				// The sample is taken before the line rather than after it,
				// because the line carries the same counts. Two reads of the
				// observation would be two different freezes of it.
				sample := obs.sample(rec.status, elapsed)

				fields := []any{
					"request_id", requestID,
					"method", r.Method,
					"path", r.URL.Path,
					"status", rec.status,
					"duration_ms", elapsed.Milliseconds(),
					"aborted", aborted != nil,
					"endpoint", sample.EndpointName,
					"outcome", string(sample.Outcome),
				}
				fields = append(fields, ident.fields()...)
				if sample.HaveTTFT {
					fields = append(fields, "ttft_ms", sample.TTFT.Milliseconds())
				}
				// Absent, not zero, when the upstream disclosed no usage: a
				// streaming caller gets a usage object only when it asked for
				// one, and a logged 0 would be counted as a real zero by any
				// query that sums this field.
				if sample.HaveUsage {
					fields = append(fields,
						"input_tokens", sample.InputTokens,
						"cached_input_tokens", sample.CachedInputTokens,
						"output_tokens", sample.OutputTokens,
					)
				}
				fields = append(fields, obs.mediaFields()...)
				slog.Info("gateway: request", fields...)

				if recorder != nil {
					recorder.Observe(sample)
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
