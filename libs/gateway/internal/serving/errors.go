package serving

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/textproto"
	"net/url"
	"sort"
	"strconv"
	"strings"
)

const (
	maxStopSequences = 4
	// maxLoggedFailureLen and maxLoggedRefusalLen bound how much of a discarded
	// upstream error body reaches the log. The body never reaches a caller, so
	// this is the only place its detail survives.
	//
	// Two caps and two levels, because the two classes are produced by different
	// things at different rates. A 5xx is rare and ours to fix, and a stack trace
	// is the whole value of the line. A 4xx is a caller's, arrives as fast as a
	// caller can loop, and one bad client must not be able to fill the log with
	// an engine's validation prose.
	maxLoggedFailureLen = 8 << 10
	maxLoggedRefusalLen = 1 << 10
	// maxNamedFields bounds how many of the caller's own field names one
	// refusal may name back at them, and minNamedFieldLen is the shortest name
	// worth looking for. A one- or two-character name — `n`, `t` — occurs inside
	// ordinary English by accident, so matching one says nothing.
	maxNamedFields   = 4
	minNamedFieldLen = 3
)

// callerFieldsKey carries the caller's own request body forward to the response
// leg, where a refusal is turned into one of this hop's objects.
//
// The bytes and not a parsed field set, because the parse is only wanted on a
// 4xx and every request would pay for it otherwise.
type callerFieldsKey struct{}

// withCallerBody attaches the bytes a caller sent, before this hop rewrites any
// of them. What is wanted downstream is the caller's own vocabulary, so the
// policy rewrite and the served-name rewrite must not be in it.
func withCallerBody(r *http.Request, body []byte) *http.Request {
	return r.WithContext(context.WithValue(r.Context(), callerFieldsKey{}, body))
}

func callerBodyFrom(ctx context.Context) []byte {
	body, _ := ctx.Value(callerFieldsKey{}).([]byte)
	return body
}

// errorEnvelope is the OpenAI error shape. Clients of an OpenAI-compatible API
// parse it, so an error from this hop has to look like an error from the thing
// it stands in front of.
type errorEnvelope struct {
	Error errorBody `json:"error"`
}

type errorBody struct {
	Message string `json:"message"`
	Type    string `json:"type"`
	Code    string `json:"code"`
}

// errorTypeFor is the error class a status carries, and the only place either
// this hop or an upstream's replaced error gets one.
//
// Derived from the status rather than passed in, because `type` is the field a
// client switches on to decide whether to retry and the two must never disagree.
// Every error here used to be `invalid_request_error` whatever its status, which
// told a client that a 503 with capacity behind it was their own bad request and
// not worth trying again.
func errorTypeFor(status int) string {
	switch {
	case status == http.StatusUnauthorized:
		return "authentication_error"
	case status == http.StatusForbidden:
		return "permission_error"
	case status == http.StatusTooManyRequests:
		return "rate_limit_error"
	case status == http.StatusServiceUnavailable:
		return "overloaded_error"
	case status >= http.StatusInternalServerError:
		return "api_error"
	default:
		return "invalid_request_error"
	}
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(errorEnvelope{
		Error: errorBody{Message: message, Type: errorTypeFor(status), Code: code},
	})
}

// tooManyStopSequences enforces the OpenAI completion limit before a request
// reaches an engine. Dynamo currently turns this client mistake into a generic
// 500, which makes a retryable-looking server failure out of a stable request.
func tooManyStopSequences(path string, raw json.RawMessage) bool {
	if (path != "/v1/chat/completions" && path != "/v1/completions") || len(raw) == 0 {
		return false
	}
	var values []json.RawMessage
	if err := json.Unmarshal(raw, &values); err != nil {
		return false
	}
	return len(values) > maxStopSequences
}

// modifyResponse is everything this hop does to a response on its way out.
//
// The three steps are in this order for a reason. The error body is replaced
// first, because replacing it sets Content-Length and the filter below has to
// see the header set that is actually going out. The headers are filtered
// second, so that nothing the engine added survives whichever of the two bodies
// is now in place. Usage is observed last, because it wraps the body and must
// wrap the final one.
func modifyResponse(resp *http.Response) error {
	if err := replaceErrorResponse(resp); err != nil {
		return err
	}
	filterResponseHeaders(resp)
	return observeUsage(resp)
}

// upstreamFailure is the answer this hop gives for one upstream status.
type upstreamFailure struct {
	// Status is what the caller is told, which is not always what the engine
	// said — see upstreamFailures. The error class follows from it through
	// errorTypeFor, so there is no second field here that could disagree.
	Status  int
	Code    string
	Message string
}

// upstreamFailures is the whole vocabulary of errors that can come from behind
// this hop. Every upstream error is answered from this table and never from the
// engine's own body.
//
// The reason is not only that an engine's message names the infrastructure that
// produced it — a path, a served name, a ClusterIP, a framework's reason phrase.
// It is that the set of errors a caller sees has to be a property of what was
// sold rather than of which engine build happens to be running: an upgrade that
// rewords a vLLM validation message is not supposed to be a change to our API,
// and a caller matching on a message is not supposed to be a caller we broke.
//
// A status absent here falls to upstreamFallback, so a status nobody anticipated
// is still one of these objects and not a passthrough. A 4xx whose body carries
// a reason listed in knownRefusals keeps this status and code but takes that
// entry's fixed message instead, which is still our string and not the engine's.
var upstreamFailures = map[int]upstreamFailure{
	http.StatusBadRequest: {
		Status: http.StatusBadRequest, Code: "invalid_request",
		Message: "The request was not accepted by this model.",
	},
	http.StatusUnprocessableEntity: {
		Status: http.StatusBadRequest, Code: "invalid_request",
		Message: "The request was not accepted by this model.",
	},
	// A credential failure behind this hop is ours, not the caller's: the
	// gateway holds that credential and the caller has never seen it. Answering
	// 401 would tell somebody to rotate a key that is working.
	http.StatusUnauthorized: {
		Status: http.StatusBadGateway, Code: "upstream_error",
		Message: "The model failed to answer this request.",
	},
	http.StatusForbidden: {
		Status: http.StatusBadGateway, Code: "upstream_error",
		Message: "The model failed to answer this request.",
	},
	// Reached only when the engine does not answer to the name this hop rewrote
	// the request to, which is a misconfigured upstream and not a caller's
	// mistake. A 404 for a path nobody serves is answered before the proxy.
	http.StatusNotFound: {
		Status: http.StatusBadGateway, Code: "upstream_error",
		Message: "The model failed to answer this request.",
	},
	http.StatusMethodNotAllowed: {
		Status: http.StatusMethodNotAllowed, Code: "method_not_allowed",
		Message: "That method is not accepted on this path.",
	},
	http.StatusRequestEntityTooLarge: {
		Status: http.StatusRequestEntityTooLarge, Code: "request_too_large",
		Message: "Request body exceeds the maximum supported size.",
	},
	http.StatusTooManyRequests: {
		Status: http.StatusTooManyRequests, Code: "rate_limited",
		Message: "Too many requests for this model. Retry shortly.",
	},
	http.StatusServiceUnavailable: {
		Status: http.StatusServiceUnavailable, Code: "no_capacity",
		Message: "This model is temporarily unavailable.",
	},
	http.StatusGatewayTimeout: {
		Status: http.StatusBadGateway, Code: "upstream_unavailable",
		Message: "The model did not respond in time.",
	},
}

// upstreamFallback answers a status the table does not name.
//
// A 4xx stays a 4xx because the class is the one thing an unrecognised status
// still says reliably — do not retry this — and a 5xx becomes 502 because
// whatever it was happened behind this hop.
func upstreamFallback(status int) upstreamFailure {
	if status < http.StatusInternalServerError {
		return upstreamFailure{
			Status: http.StatusBadRequest, Code: "invalid_request",
			Message: "The request was not accepted by this model.",
		}
	}
	return upstreamFailure{
		Status: http.StatusBadGateway, Code: "upstream_error",
		Message: "The model failed to answer this request.",
	}
}

// knownRefusals are the upstream 4xx reasons this hop recognises and answers
// with a message of its own.
//
// The needle is looked for in the upstream body and the message is a constant
// here, so a match emits a string from this table and never one from the
// engine. The body is consulted, as callerFieldsNamedIn consults it, and not
// quoted. Needles are lower case and matched against the lowered body, so an
// engine that prefixes an exception class or changes case still matches.
var knownRefusals = []struct {
	needle  string
	message string
}{
	{needle: "invalid video file", message: "invalid video file"},
}

// knownRefusalIn returns the fixed message for the first known reason the
// upstream body mentions.
func knownRefusalIn(detail string) (string, bool) {
	if detail == "" {
		return "", false
	}
	lowered := strings.ToLower(detail)
	for _, refusal := range knownRefusals {
		if strings.Contains(lowered, refusal.needle) {
			return refusal.message, true
		}
	}
	return "", false
}

// replaceErrorResponse substitutes this hop's own error object for whatever an
// upstream sent, discarding the upstream body entirely.
//
// It replaces rather than normalises. Wrapping the engine's own object in the
// OpenAI envelope was enough to make the shape right and not enough to make the
// content ours: the message, the type and the code were still the engine's, an
// oversized body was streamed through untouched, and a body that already had an
// `error` key passed unread. The engine's detail is logged here instead, which
// is the only place it is wanted.
//
// A response that is not an error is left alone, which is every response on the
// hot path. Nothing here runs for a 200.
func replaceErrorResponse(resp *http.Response) error {
	if resp.StatusCode < http.StatusBadRequest {
		return nil
	}

	status := resp.StatusCode
	detail, readErr := readUpstreamError(resp)
	logUpstreamError(resp, detail, readErr)

	failure, known := upstreamFailures[status]
	if !known {
		failure = upstreamFallback(status)
	}

	message := failure.Message
	reason, recognised := "", false
	if status < http.StatusInternalServerError && failure.Status < http.StatusInternalServerError {
		reason, recognised = knownRefusalIn(detail)
	}
	switch {
	case recognised:
		message = reason
	case status < http.StatusInternalServerError && resp.Request != nil:
		if named := callerFieldsNamedIn(detail, callerBodyFrom(resp.Request.Context())); len(named) > 0 {
			message += " Check these fields: " + strings.Join(named, ", ") + "."
		}
	}

	body, err := json.Marshal(errorEnvelope{
		Error: errorBody{
			Message: message,
			Type:    errorTypeFor(failure.Status),
			Code:    failure.Code,
		},
	})
	if err != nil {
		return err
	}

	resp.StatusCode = failure.Status
	resp.Status = strconv.Itoa(failure.Status) + " " + http.StatusText(failure.Status)
	resp.Body = io.NopCloser(strings.NewReader(string(body)))
	resp.ContentLength = int64(len(body))
	resp.Header.Set("Content-Type", "application/json")
	resp.Header.Set("Content-Length", strconv.Itoa(len(body)))
	// The engine's body is gone, and so is any header that described it. A
	// Content-Encoding left behind here would claim the fixed object above is
	// gzipped.
	resp.Header.Del("Content-Encoding")
	return nil
}

// readUpstreamError drains the body that is about to be discarded, then closes
// it. Read under a cap and never returned as an error: a body this hop cannot
// read is still a body it is not going to forward, and failing here would turn
// a described upstream error into an undescribed proxy error.
func readUpstreamError(resp *http.Response) (string, error) {
	if resp.Body == nil {
		return "", nil
	}
	limit := int64(maxLoggedRefusalLen)
	if resp.StatusCode >= http.StatusInternalServerError {
		limit = maxLoggedFailureLen
	}
	detail, err := io.ReadAll(io.LimitReader(resp.Body, limit))
	_ = resp.Body.Close()
	return string(detail), err
}

// logUpstreamError records the discarded body at the level its class deserves.
// The read and the log are two calls so that the same bytes serve both.
func logUpstreamError(resp *http.Response, detail string, readErr error) {
	level := slog.LevelWarn
	if resp.StatusCode >= http.StatusInternalServerError {
		level = slog.LevelError
	}
	attrs := []any{"status", resp.StatusCode}
	if resp.Request != nil && resp.Request.URL != nil {
		attrs = append(attrs, "path", resp.Request.URL.Path)
	}
	if readErr != nil {
		attrs = append(attrs, "read_err", readErr)
	}
	attrs = append(attrs, "upstream_body", detail)
	slog.Log(context.Background(), level,
		"gateway: upstream error, answering with this hop's own object", attrs...)
}

// callerFieldsNamedIn is where the line between preserved and replaced 4xx
// detail actually sits, and it is worth stating exactly.
//
// A 4xx is often the only thing telling a caller what they did wrong, so
// answering every one of them with the same sentence makes the endpoint
// undebuggable. But an engine's prose is the leak: it names Rust types, serde
// variants, module paths, ClusterIPs and framework reason phrases, and it
// rewords itself on an upgrade. Both halves cannot be had from the same string.
//
// So the engine's body is never quoted, only consulted. It is used as a
// selector over the caller's own vocabulary: a top-level field name the caller
// themselves wrote is reported back to them when the upstream's complaint
// mentions it, and nothing else is. The output alphabet is therefore fixed
// strings plus tokens the caller sent us in this very request, which makes a
// string the caller never wrote impossible to emit rather than merely filtered
// — the eight needles the conformance profiles list are a symptom of that class
// and not the definition of it.
//
// knownRefusals is the other consultation and has the same shape: a reason the
// body mentions selects a fixed message from our own table. When one matches,
// its message is the answer and no field names are appended.
//
// The cost is honest and stated: an engine that names only its own type, as
// serde does for an untagged enum, contributes nothing, and the caller gets the
// generic sentence. There is nothing in such a message that is ours to say. The
// full text is in the log at warn, capped, which is the one place it is wanted.
func callerFieldsNamedIn(detail string, callerBody []byte) []string {
	if detail == "" || len(callerBody) == 0 {
		return nil
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(callerBody, &fields); err != nil {
		return nil
	}
	lowered := strings.ToLower(detail)
	named := make([]string, 0, maxNamedFields)
	for name := range fields {
		if len(name) < minNamedFieldLen || neverNamedBack[name] {
			continue
		}
		if mentionsToken(lowered, strings.ToLower(name)) {
			named = append(named, name)
		}
	}
	sort.Strings(named)
	if len(named) > maxNamedFields {
		named = named[:maxNamedFields]
	}
	return named
}

// neverNamedBack are the fields excluded from that report even though a caller
// sent them.
//
// `model` is the routing key, and it is also an ordinary English word that
// appears in nearly every engine message — so matching it names a field on
// almost every refusal and says nothing. It is the one field this hop rewrites
// besides, so the string the engine complained about is not always the string
// the caller wrote, and a caller sent to check it would be sent to check ours.
var neverNamedBack = map[string]bool{"model": true}

// mentionsToken reports whether a name occurs in the text as a whole token.
//
// Substring matching alone would report `tool` for a message about
// `tool_choice`, which sends a caller to a field that was fine.
func mentionsToken(text, name string) bool {
	for offset := 0; ; {
		index := strings.Index(text[offset:], name)
		if index < 0 {
			return false
		}
		start := offset + index
		end := start + len(name)
		if !isNameByte(text, start-1) && !isNameByte(text, end) {
			return true
		}
		offset = start + 1
	}
}

func isNameByte(text string, index int) bool {
	if index < 0 || index >= len(text) {
		return false
	}
	c := text[index]
	return c == '_' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9'
}

// forwardedHeaders is every response header an upstream may put in front of a
// caller. Everything else is deleted.
//
// An allowlist and not a denylist, because the set of headers a caller needs is
// small, closed and ours to state, while the set an engine might add is neither
// — a framework's `Server`, a tracing header, a debug header somebody turned on
// for an afternoon. A denylist would have to be edited every time one of those
// appeared, and would be wrong in the window before somebody noticed.
//
// Keys are canonical. Go canonicalises on parse and on Set, so a comparison
// against these is exact.
var forwardedHeaders = map[string]bool{
	// The body: what it is, how long, how encoded, and what to call it if a
	// caller saves it. /v1/audio/speech returns a file and needs the last one.
	"Content-Type":        true,
	"Content-Length":      true,
	"Content-Encoding":    true,
	"Content-Language":    true,
	"Content-Disposition": true,
	// Streaming. Cache-Control: no-cache is part of an SSE response, and
	// X-Accel-Buffering: no is how an engine tells an nginx in front of it not
	// to buffer the stream — dropping it would turn a token-by-token answer
	// into one long pause wherever such a proxy sits between here and a caller.
	"Cache-Control":     true,
	"X-Accel-Buffering": true,
	// Where a redirect points, rewritten to a path by relativizeLocation before
	// it gets here.
	"Location":         true,
	"Content-Location": true,
	// Retry timing. Meaningful to a caller on a 429 or a 503 and worth nothing
	// to anyone else.
	"Retry-After": true,
}

// forwardedHeaderPrefixes are the header families that pass by prefix because
// their members are named after a quota rather than fixed by a spec.
var forwardedHeaderPrefixes = []string{"x-ratelimit-", "ratelimit-"}

// filterResponseHeaders deletes every response header that is not forwarded,
// and reduces the ones that carry a URL to a path.
//
// This is the boundary. Everything behind it — the tunnel's synthetic
// authority, the agent, the frontend's ClusterIP, the engine — is entitled to
// describe itself in a header, and none of that is entitled to leave here.
func filterResponseHeaders(resp *http.Response) {
	// Trailers as well as headers. Nothing on the OpenAI surface sends one, so
	// a trailer here is something a framework added, arriving after the body and
	// through a path an allowlist over resp.Header alone does not cover.
	resp.Trailer = nil

	relativizeLocation(resp.Header)
	for name := range resp.Header {
		if forwardedHeaders[name] {
			continue
		}
		lowered := strings.ToLower(name)
		forwarded := false
		for _, prefix := range forwardedHeaderPrefixes {
			if strings.HasPrefix(lowered, prefix) {
				forwarded = true
				break
			}
		}
		if !forwarded {
			resp.Header.Del(name)
		}
	}
}

// relativizeLocation strips the scheme and authority off any header that
// carries a URL.
//
// A redirect is where the address of the thing that answered leaks by
// construction. Neither proxy on this path rewrites Location, and the authority
// the frontend sees is the one the agent dialled — a ClusterIP name — so a
// framework that answers `/v1/models/` with a 307 built from its own Host
// publishes that name to whoever asked. Reducing the value to a path keeps the
// redirect working and says nothing about who served it.
//
// A value that is already relative is left exactly as it is, including a
// malformed one: this is not the place to decide an engine's header is invalid,
// and a value with no authority in it has nothing to strip.
func relativizeLocation(header http.Header) {
	for _, name := range []string{"Location", "Content-Location"} {
		values, present := header[textproto.CanonicalMIMEHeaderKey(name)]
		if !present {
			continue
		}
		rewritten := make([]string, 0, len(values))
		for _, value := range values {
			parsed, err := url.Parse(value)
			if err != nil || (parsed.Scheme == "" && parsed.Host == "") {
				rewritten = append(rewritten, value)
				continue
			}
			relative := parsed.RequestURI()
			if parsed.Fragment != "" {
				relative += "#" + parsed.EscapedFragment()
			}
			rewritten = append(rewritten, relative)
		}
		header[textproto.CanonicalMIMEHeaderKey(name)] = rewritten
	}
}

type modelObject struct {
	ID      string `json:"id"`
	Object  string `json:"object"`
	OwnedBy string `json:"owned_by"`
}

type modelList struct {
	Object string        `json:"object"`
	Data   []modelObject `json:"data"`
}

// modelOwner is what every model in the catalog is owned by.
//
// A constant, and deliberately the same one OpenAI returns for its own base
// models. It used to name this project, which put the name of the thing serving
// the request on the one response a caller can read without sending one. What
// sits in front of this hop owns the catalog a customer sees; this list exists
// so the gateway can answer for the whole table, and it has nothing to say
// about who runs it.
const modelOwner = "system"

// writeModelList answers GET /v1/models from the routing table.
//
// Deliberately bare: context length, quantization and display names are catalog
// facts, and hangar serves models rather than describing them. Whatever sits in
// front of this hop owns its own catalog.
func writeModelList(w http.ResponseWriter, names []string) {
	sort.Strings(names)
	data := make([]modelObject, 0, len(names))
	for _, name := range names {
		data = append(data, modelObject{ID: name, Object: "model", OwnedBy: modelOwner})
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(modelList{Object: "list", Data: data})
}
