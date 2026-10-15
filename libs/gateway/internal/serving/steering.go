package serving

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"unicode"
)

// steeringHeaderPrefix is the serving runtime's own header namespace. Dynamo reads a
// worker pin, a data-parallel rank, a queue priority, a request id, a session binding
// and free-form router metadata out of it — `x-dynamo-meta-policy-class` chooses a
// queue class — and adds names to it between releases, so it is matched as a prefix
// rather than enumerated.
const steeringHeaderPrefix = "x-dynamo-"

// steeringHeaders are the names outside that prefix which Dynamo 1.5 reads for the
// same purposes: the unprefixed aliases of the worker and rank pins, and the
// coding-agent session headers that bind a session to one worker once session
// affinity is turned on. The session headers carry the names of the client tools that
// send them because that is how Dynamo reads them; the gateway gives no client special
// treatment and strips every one alike. With affinity off these do nothing, which is
// exactly why they cannot be left to pass: they would start pinning workers the day it
// is enabled, with nothing in this repository changing.
//
// `x-tenant-id` is the odd one: Dynamo (since 1.4) reads it as the request's
// `cache_salt` and lets it override the body's. Passed through, it would let a caller
// replace the tenant salt this hop writes with one naming another tenant's cache, or
// with a constant that puts every image back on one identity.
var steeringHeaders = map[string]bool{
	"x-worker-instance-id":          true,
	"x-prefill-instance-id":         true,
	"x-dp-rank":                     true,
	"x-data-parallel-rank":          true,
	"x-prefill-dp-rank":             true,
	"thread-id":                     true,
	"x-session-id":                  true,
	"x-parent-session-id":           true,
	"x-claude-code-session-id":      true,
	"x-claude-code-agent-id":        true,
	"x-claude-code-parent-agent-id": true,
	"x-codex-parent-thread-id":      true,
	"x-codex-turn-metadata":         true,
	"x-tenant-id":                   true,
}

// steeringHints are the `nvext` fields Dynamo turns into a routing decision: the body
// spelling of the worker and rank pins, the priority and queue-tier hints under
// `agent_hints` (every one of which feeds the scheduler), taint constraints, and the
// latency targets the router scores against.
//
// `cache_salt` is not here. It narrows prefix sharing for the request that carries it
// and nobody else, which is a caller isolating itself rather than steering anyone.
var steeringHints = []string{
	"backend_instance_id",
	"prefill_worker_id",
	"decode_worker_id",
	"dp_rank",
	"prefill_dp_rank",
	"agent_hints",
	"routing_constraints",
	"router",
}

// stripSteeringHeaders deletes every caller header the serving runtime would read as
// an instruction about which worker runs a request, when, or in which queue, and
// returns the names it deleted.
//
// Where a request runs inside a cluster is the runtime's decision, made on queue depth
// and cache residency no caller can see, and priority is something that is sold
// rather than asserted. httputil.ReverseProxy copies inbound headers verbatim, so
// without this any caller could pin a prefill worker or jump the queue ahead of every
// other tenant with a header they made up.
//
// It must run on the inbound headers, before the proxy's Rewrite. Headers this hop
// stamps itself — the credential map on the outbound leg — are written after it and
// are untouched by it.
func stripSteeringHeaders(header http.Header) []string {
	var stripped []string
	for name := range header {
		if isSteeringHeader(name) {
			delete(header, name)
			stripped = append(stripped, name)
		}
	}
	return stripped
}

// isSteeringHeader matches case-insensitively and reads an underscore as a hyphen. Go
// only canonicalises a name it can parse, and a proxy in front of the frontend may
// fold `x_dynamo_request_id` into the name the runtime reads.
func isSteeringHeader(name string) bool {
	name = strings.Map(func(r rune) rune {
		if r == '_' {
			return '-'
		}
		return unicode.ToLower(r)
	}, name)
	return strings.HasPrefix(name, steeringHeaderPrefix) || steeringHeaders[name]
}

// withoutSteeringHints returns the body with every routing field removed from its
// `nvext`, and the fields it removed.
//
// It is called only when the envelope saw an `nvext`, so a request without one pays
// nothing here. A request whose `nvext` carries no routing field pays one top-level
// decode and is forwarded as the exact bytes that arrived. Only a request that
// actually steers is re-encoded, one level deep like withModel.
//
// The key is matched exactly, as the runtime's decoder matches it, and not with the
// case folding encoding/json applies to a struct: an `NVEXT` is a field the runtime
// does not read. A repeated `nvext` is always re-encoded, whatever it carries, because
// two decoders need not agree on which copy wins, and forwarding both would let the
// copy this hop checked hide the one the runtime reads.
//
// An `nvext` emptied by this is removed rather than sent as `{}`.
func withoutSteeringHints(body []byte) ([]byte, []string, error) {
	fields, repeated, ok := topLevelFields(body)
	if !ok {
		return body, nil, nil
	}
	raw, present := fields["nvext"]
	if !present {
		return body, nil, nil
	}

	var removed []string
	if repeated {
		removed = append(removed, "nvext")
	}
	var hints map[string]json.RawMessage
	if err := json.Unmarshal(raw, &hints); err != nil {
		// Not an object: the runtime refuses it as invalid nvext and routes nothing.
		hints = nil
	}
	for _, key := range steeringHints {
		if _, set := hints[key]; set {
			delete(hints, key)
			removed = append(removed, "nvext."+key)
		}
	}
	if len(removed) == 0 {
		return body, nil, nil
	}

	switch {
	case hints == nil:
	case len(hints) == 0:
		delete(fields, "nvext")
	default:
		encoded, err := json.Marshal(hints)
		if err != nil {
			return nil, nil, err
		}
		fields["nvext"] = encoded
	}
	rewritten, err := json.Marshal(fields)
	if err != nil {
		return nil, nil, err
	}
	return rewritten, removed, nil
}

// topLevelFields decodes a JSON object one level deep, and reports whether `nvext`
// appeared in it more than once. A map alone cannot say: it keeps the last copy and
// hides that there was another.
func topLevelFields(body []byte) (fields map[string]json.RawMessage, nvextRepeated bool, ok bool) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	if open, err := decoder.Token(); err != nil || open != json.Delim('{') {
		return nil, false, false
	}
	fields = make(map[string]json.RawMessage)
	for decoder.More() {
		token, err := decoder.Token()
		if err != nil {
			return nil, false, false
		}
		key, _ := token.(string)
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, false, false
		}
		if _, seen := fields[key]; seen && key == "nvext" {
			nvextRepeated = true
		}
		fields[key] = value
	}
	return fields, nvextRepeated, true
}
