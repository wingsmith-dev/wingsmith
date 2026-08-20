package serving

import (
	"net/http"
	"strings"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

// The output modalities the plane puts on an upstream. Empty is text.
const (
	modalityText  = "text"
	modalityImage = "image"
	modalityVideo = "video"
)

// pathAnswered says whether an upstream that emits `modality` answers a request to `path`.
//
// The rule, from what Dynamo's frontend serves in front of each kind of worker:
//
//   - A text upstream answers everything except the image and video generation paths. It has
//     never been refused a path before, and the only paths added here are the ones no chat
//     engine serves.
//   - An image upstream answers `/v1/images/*` and `/v1/chat/completions`. The second was
//     verified on site-a on 2026-09-23: a chat request to an omni image model returns the image
//     as an `image_url` content part. Everything else — completions, embeddings, messages,
//     responses, video — has no handler in front of a diffusion worker.
//   - A video upstream answers `/v1/videos` and nothing else. Chat against a video model was
//     not verified, so it is refused rather than proxied into an answer nobody has seen.
//
// An unknown modality answers everything: a plane newer than this gateway is not a reason to
// refuse traffic the gateway cannot reason about.
func pathAnswered(modality, path string) bool {
	images := strings.HasPrefix(path, "/v1/images/")
	videos := path == "/v1/videos" || strings.HasPrefix(path, "/v1/videos/")
	switch modality {
	case "", modalityText:
		return !images && !videos
	case modalityImage:
		return images || path == "/v1/chat/completions"
	case modalityVideo:
		return videos
	default:
		return true
	}
}

// routeModality is the one modality a route's upstreams share, or "" (text) when it has none.
//
// An endpoint whose upstreams disagree is a declaration mistake; it reads as the first upstream's,
// and the refusal below checks every upstream rather than this, so a mixed route still refuses
// only a path none of its upstreams answers.
func routeModality(route routing.Route) string {
	for _, upstream := range route.Upstreams {
		if upstream.OutputModality != "" {
			return upstream.OutputModality
		}
	}
	return modalityText
}

// stageCheckModality refuses a request to a path the endpoint's engine does not answer.
//
// Before the endpoint's terms and before Pick, for the reason stageCheckPolicy gives: the
// answer is a property of the name that was sold, and it must not depend on which cluster the
// request would have landed on. A 404, because the path is what is wrong — the model exists and
// would answer a different URL — and a 404 is what an OpenAI-compatible server says about a URL
// it has no handler for. A route with no upstreams at all is left to Pick's 503: "nowhere to go
// right now" and "this model does not do that" are different answers.
func stageCheckModality(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	if len(req.route.Upstreams) == 0 {
		return r, true
	}
	for _, upstream := range req.route.Upstreams {
		if pathAnswered(upstream.OutputModality, r.URL.Path) {
			return r, true
		}
	}
	MarkOutcome(r, metrics.OutcomeClientError)
	writeError(w, http.StatusNotFound, "unsupported_path",
		"The model '"+req.envelope.Model+"' generates "+routeModality(req.route)+
			" and does not answer "+r.URL.Path+".")
	return r, false
}

// stageVideoRetrieval answers the async half of the OpenAI video API, which nothing behind this
// hop implements.
//
// Dynamo 1.5.0's frontend serves `POST /v1/videos` synchronously — the response is the finished
// video — and has no job store, so `GET /v1/videos/{id}`, its `/content`, a listing and a `DELETE`
// have nothing to read or cancel. Answered before the body is read, because these requests carry
// no body and no `model`, and without this they reached the JSON parse and came back as a 400
// about a body the caller never sent. A 404 `unsupported_path`, as stageCheckModality answers a
// path an engine has no handler for, with a message that says what to do instead.
//
// `POST` to any videos path is left alone, including Dynamo's experimental `/v1/videos/stream`.
func stageVideoRetrieval(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	if r.Method == http.MethodPost {
		return r, true
	}
	if r.URL.Path != "/v1/videos" && !strings.HasPrefix(r.URL.Path, "/v1/videos/") {
		return r, true
	}
	markEndpoint(r, metrics.EndpointUnknown, "")
	MarkOutcome(r, metrics.OutcomeClientError)
	writeError(w, http.StatusNotFound, "unsupported_path",
		"Video generation is synchronous: POST /v1/videos returns the finished video. "+
			"There is no video job to retrieve, list or delete.")
	return r, false
}
