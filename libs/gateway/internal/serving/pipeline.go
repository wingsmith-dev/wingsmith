package serving

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// requestState is what one stage of withRouting's pipeline learns and the
// next one needs. It is a plain struct threaded through a straight-line call
// sequence within one request's own goroutine, not carried on the request's
// context: nothing on it is read by code the pipeline does not call itself,
// which is what tells it apart from `observation` (see observe.go), whose
// fields are read by the reverse proxy's own callbacks running later and so
// have to travel through the context instead.
type requestState struct {
	body        []byte
	isMultipart bool
	boundary    string
	envelope    modelEnvelope
	route       routing.Route
	upstream    routing.Upstream
}

// stage is one step of routing a request to an upstream. It returns the
// request to continue with — a stage may wrap it, as stageParseEnvelope does
// when it attaches the caller's own bytes for later error rendering — and
// false once it has already written the response, which stops the pipeline.
type stage func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool)

// routingStages is the ordered pipeline withRouting runs for every request.
// Each stage either narrows what the next one needs to know or refuses the
// request outright; the first to write a response ends the chain.
func routingStages(resolver Resolver) []stage {
	return []stage{
		stageCatalog(resolver),
		stageReadBody,
		stageParseEnvelope,
		stageValidateModel,
		stageValidateStopSequences,
		stageResolve(resolver),
		stageStripSteeringHints,
		stageCheckPolicy,
		stageRewritePolicy,
		stageMediaCacheSalt,
		stagePickUpstream,
		stageRewriteServedModel,
		stageStripSteeringHeaders,
		stageDeploymentHeader,
		stageForward,
	}
}

// runPipeline runs a request through an ordered set of stages, stopping at
// the first one that has already written a response.
func runPipeline(stages []stage, w http.ResponseWriter, r *http.Request) {
	req := &requestState{}
	for _, s := range stages {
		var ok bool
		r, ok = s(w, r, req)
		if !ok {
			return
		}
	}
}

// stageCatalog answers GET /v1/models from the table, because the gateway —
// not any one cluster — is what knows the whole set. It is the only stage
// that can be the whole of a request; every stage after it assumes a body
// worth routing.
func stageCatalog(resolver Resolver) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/models" {
			return r, true
		}
		markEndpoint(r, metrics.EndpointCatalog, "")
		writeModelList(w, resolver.Names())
		return r, false
	}
}

// stageReadBody buffers the request body under the size cap.
//
// Before the endpoint is known a request has no endpoint label, and it is not
// given the model string it asked for: a free-text label is an unbounded axis
// handed to whoever is calling.
func stageReadBody(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	markEndpoint(r, metrics.EndpointUnknown, "")
	body, ok := readBody(w, r)
	if !ok {
		return r, false
	}
	req.body = body
	return r, true
}

// stageParseEnvelope decides how the routing key is carried and extracts it.
//
// The choice is made once and used twice: here to read it, and after Pick to
// write the served name back. A multipart body is not a lesser case of a JSON
// one — there is no object to unmarshal — so the form travels with the
// request rather than being re-derived.
//
// The caller's own bytes are attached to the request before either rewrite
// downstream touches them, so that a refusal from an engine can be answered
// in the caller's own vocabulary rather than the engine's. See
// callerFieldsNamedIn.
func stageParseEnvelope(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	boundary, isMultipart := multipartContentType(r.Header.Get("Content-Type"))
	req.boundary = boundary
	req.isMultipart = isMultipart

	if isMultipart {
		model, err := modelFromMultipart(req.body, boundary)
		if err != nil {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusBadRequest, "invalid_multipart",
				"Request body is not a readable multipart form.")
			return r, false
		}
		req.envelope.Model = model
	} else if err := json.Unmarshal(req.body, &req.envelope); err != nil {
		MarkOutcome(r, metrics.OutcomeClientError)
		writeError(w, http.StatusBadRequest, "invalid_json", "Request body is not valid JSON.")
		return r, false
	}

	if !isMultipart {
		r = withCallerBody(r, req.body)
	}
	return r, true
}

// stageValidateModel refuses a request that named no model at all.
func stageValidateModel(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	if req.envelope.Model == "" {
		MarkOutcome(r, metrics.OutcomeClientError)
		writeError(w, http.StatusBadRequest, "missing_model", "You must provide a model identifier.")
		return r, false
	}
	return r, true
}

// stageValidateStopSequences enforces the provider limit on `stop`.
func stageValidateStopSequences(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	if tooManyStopSequences(r.URL.Path, req.envelope.Stop) {
		MarkOutcome(r, metrics.OutcomeClientError)
		writeError(w, http.StatusBadRequest, "too_many_stop_sequences",
			"You may provide at most four stop sequences.")
		return r, false
	}
	return r, true
}

// stageResolve turns the request's model into the endpoint serving it.
func stageResolve(resolver Resolver) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		route, err := resolver.Resolve(req.envelope.Model)
		if err != nil {
			if errors.Is(err, routing.ErrUnknownEndpoint) {
				MarkOutcome(r, metrics.OutcomeClientError)
				writeError(w, http.StatusNotFound, "model_not_found",
					"The model '"+req.envelope.Model+"' is not served here.")
				return r, false
			}
			MarkOutcome(r, metrics.OutcomeInternalError)
			slog.Error("gateway: resolution failed", "model", req.envelope.Model, "err", err)
			writeError(w, http.StatusInternalServerError, "routing_error", "Unable to route this request.")
			return r, false
		}
		req.route = route
		markEndpoint(r, route.ID, route.Name)
		return r, true
	}
}

// stageStripSteeringHints removes the caller's own routing hints from
// `nvext`.
//
// Before the endpoint's own terms and not after them, so that a field a
// plane-authored rule writes into `nvext` is not taken back out as if a
// caller had sent it.
func stageStripSteeringHints(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	if len(req.envelope.NvExt) == 0 {
		return r, true
	}
	scrubbed, removed, err := withoutSteeringHints(req.body)
	if err != nil {
		MarkOutcome(r, metrics.OutcomeInternalError)
		slog.Error("gateway: could not remove routing hints from the request",
			"model", req.envelope.Model, "err", err)
		writeError(w, http.StatusInternalServerError, "routing_error", "Unable to route this request.")
		return r, false
	}
	if len(removed) > 0 {
		slog.Debug("gateway: removed caller routing hints", "fields", removed)
	}
	req.body = scrubbed
	return r, true
}

// stageCheckPolicy refuses a request the endpoint's own terms do not allow.
//
// Before an upstream is chosen. A policy is a property of the name that was
// sold, so every upstream behind it must answer a request the same way —
// checking after Pick would let the same call be a 400 or a 200 depending on
// which cluster took it.
//
// The path goes in because a term is about one protocol and not about the
// endpoint's whole surface. A rule measured against chat completions used to
// be enforced against /v1/embeddings, /v1/messages and /v1/responses as well,
// which refused requests nobody had measured.
func stageCheckPolicy(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	if code, message, ok := checkPolicy(req.route.Policy, r.URL.Path, req.body); !ok {
		MarkOutcome(r, metrics.OutcomeClientError)
		writeError(w, http.StatusBadRequest, code, message)
		return r, false
	}
	return r, true
}

// stageRewritePolicy applies the half of a policy that writes instead of
// refusing: a field the provider treats as having a default, spelled out; a
// field the provider accepts and ignores, taken back out. Both so that the
// engine behind this endpoint reads what the provider's own frontend would
// have.
//
// Beside the check, because those terms are properties of the endpoint too —
// the served-model rewrite below is the one rewrite that belongs after Pick,
// because the name it writes is the upstream's and not the endpoint's.
//
// A request with nothing to write is returned as the exact bytes that
// arrived, so the ordinary path costs one map walk and no re-encoding.
func stageRewritePolicy(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	conformed, err := rewriteBody(req.route.Policy, r.URL.Path, req.body)
	if err != nil {
		MarkOutcome(r, metrics.OutcomeInternalError)
		slog.Error("gateway: could not apply the endpoint's request terms",
			"model", req.envelope.Model, "err", err)
		writeError(w, http.StatusInternalServerError, "routing_error", "Unable to route this request.")
		return r, false
	}
	req.body = conformed
	return r, true
}

// stageMediaCacheSalt puts back the media identity the serving runtime drops
// on the way to a disaggregated decode worker, as the one field that survives
// the handoff into its prefix-cache block hash. See `mediaIdentity`.
//
// After the endpoint's terms rather than before them, so a rule that writes a
// media part is inside the identity, and before `Pick` because the salt is a
// property of the request and not of the cluster that takes it — the same
// body has to hash the same way wherever it lands.
func stageMediaCacheSalt(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	if !hasMediaPart(req.body) {
		return r, true
	}
	salted, written, err := withMediaCacheSalt(req.body)
	if err != nil {
		MarkOutcome(r, metrics.OutcomeInternalError)
		slog.Error("gateway: could not derive the media cache salt",
			"model", req.envelope.Model, "err", err)
		writeError(w, http.StatusInternalServerError, "routing_error", "Unable to route this request.")
		return r, false
	}
	if written {
		slog.Debug("gateway: wrote the media cache salt", "model", req.envelope.Model)
	}
	req.body = salted
	return r, true
}

// stagePickUpstream chooses which upstream behind the endpoint takes this
// request.
func stagePickUpstream(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	upstream, err := req.route.Pick()
	if err != nil {
		MarkOutcome(r, metrics.OutcomeNoUpstream)
		slog.Error("gateway: no upstream for endpoint", "model", req.envelope.Model, "err", err)
		writeError(w, http.StatusServiceUnavailable, "no_capacity",
			"The model '"+req.envelope.Model+"' is temporarily unavailable.")
		return r, false
	}
	req.upstream = upstream
	return r, true
}

// stageRewriteServedModel rewrites the caller's model to the name the engine
// behind the chosen upstream answers to.
//
// After Pick because the served name belongs to the upstream and not to the
// endpoint — one endpoint can front two clusters running different serving
// configurations, and each wants its own name.
func stageRewriteServedModel(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	served := req.upstream.ServedModelName
	if served == "" || served == req.envelope.Model {
		return r, true
	}
	var rewritten []byte
	var err error
	if req.isMultipart {
		var contentType string
		rewritten, contentType, err = withModelMultipart(req.body, req.boundary, served)
		if err == nil {
			r.Header.Set("Content-Type", contentType)
		}
	} else {
		rewritten, err = withModel(req.body, served)
	}
	if err != nil {
		MarkOutcome(r, metrics.OutcomeInternalError)
		slog.Error("gateway: could not rewrite the model name",
			"model", req.envelope.Model, "served", served, "err", err)
		writeError(w, http.StatusInternalServerError, "routing_error", "Unable to route this request.")
		return r, false
	}
	req.body = rewritten
	return r, true
}

// stageStripSteeringHeaders removes the runtime's own steering headers from
// the caller's request before anything this hop stamps is written. See
// stripSteeringHeaders.
func stageStripSteeringHeaders(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	if stripped := stripSteeringHeaders(r.Header); len(stripped) > 0 {
		slog.Debug("gateway: removed caller routing headers", "headers", stripped)
	}
	return r, true
}

// stageDeploymentHeader forwards the deployment this request was resolved to,
// so the agent can hand it to that model's own frontend instead of asking
// each frontend what it serves. The gateway is the only hop that has parsed
// the body, so it is the only hop that can name it.
//
// Written unconditionally, and deleted just as unconditionally when there is
// nothing to write. That is a security requirement and not tidiness:
// httputil.ReverseProxy clones the inbound headers verbatim onto the outbound
// request, so a value a caller sent under this name would ride all the way
// into the cluster and choose which model's frontend answers them — a caller
// picking somebody else's deployment, across tenants, using a header they
// made up. The Set covers the case where the gateway has an answer and the
// Del covers the case where it does not, and between them the caller's own
// value never survives this line.
//
// Not the per-credential header map on the pooled proxy, which is where every
// other stamped header lives: that map is built once per handler, and the
// deployment differs per request.
func stageDeploymentHeader(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	if req.upstream.DeploymentID != "" {
		r.Header.Set(tunnel.HeaderDeploymentID, req.upstream.DeploymentID)
	} else {
		r.Header.Del(tunnel.HeaderDeploymentID)
	}
	return r, true
}

// stageForward hands the request to the chosen upstream. It is always the
// last stage: everything before it either refuses the request or narrows
// what this one needs to know.
//
// Replayable, using the buffer that was read to find the model rather than a
// second copy of it. Without a GetBody, HTTP/2 refuses to retry anything with
// a body after an agent sends GOAWAY — which is every completion there is,
// and they are the requests it is cheapest to get right because the agent
// never processed them.
func stageForward(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	r = tunnel.Replayable(r, req.body)
	r.Header.Del("Content-Length")
	r.TransferEncoding = nil

	markUpstream(r, req.upstream.DeploymentID, req.upstream.ClusterName)
	release := inFlight(r)
	defer release()

	req.upstream.Handler().ServeHTTP(w, r)
	return r, false
}
