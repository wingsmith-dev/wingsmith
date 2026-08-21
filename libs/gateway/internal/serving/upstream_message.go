package serving

import (
	"context"
	"encoding/json"
	"net/http"
	"regexp"
	"strings"
	"unicode/utf8"
)

// maxQuotedUpstreamLen caps how much of an engine's message reaches a caller. Long enough for a
// validation message and its context; short enough that a message is never a dump.
const maxQuotedUpstreamLen = 500

// redacted is what stands in for anything that names the infrastructure behind this hop.
const redacted = "[redacted]"

// servedNameKey carries the name this hop rewrote `model` to, and the name the caller used, forward
// to the response leg, so an engine's message about its own served name reads back in the caller's.
type servedNameKey struct{}

type servedName struct {
	served string
	public string
}

func withServedName(r *http.Request, served, public string) *http.Request {
	return r.WithContext(context.WithValue(r.Context(), servedNameKey{}, servedName{served: served, public: public}))
}

func servedNameFrom(ctx context.Context) servedName {
	name, _ := ctx.Value(servedNameKey{}).(servedName)
	return name
}

// upstreamMessage extracts the human message from an engine's JSON error body, or "" when the body
// is not a JSON error this hop recognises.
//
// The shapes are the ones behind this hop: Dynamo's frontend writes a flat
// `{"message","type","code"}`; vLLM and anything OpenAI-shaped write `{"error":{"message":...}}`;
// older Dynamo builds wrote `{"error":"..."}`; FastAPI writes `{"detail":"..."}` or, for a
// validation failure, `{"detail":[{"msg":...}]}`. Prose is not quoted: a body that is not JSON is
// whatever a framework's default handler wrote, and that is the body most likely to be a trace.
func upstreamMessage(detail string) string {
	var body struct {
		Error   json.RawMessage `json:"error"`
		Message string          `json:"message"`
		Detail  json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal([]byte(detail), &body); err != nil {
		return ""
	}
	if len(body.Error) > 0 {
		var nested struct {
			Message string `json:"message"`
		}
		if json.Unmarshal(body.Error, &nested) == nil && nested.Message != "" {
			return nested.Message
		}
		var flat string
		if json.Unmarshal(body.Error, &flat) == nil && flat != "" {
			return flat
		}
	}
	if body.Message != "" {
		return body.Message
	}
	if len(body.Detail) > 0 {
		var flat string
		if json.Unmarshal(body.Detail, &flat) == nil {
			return flat
		}
		var items []struct {
			Msg string `json:"msg"`
		}
		if json.Unmarshal(body.Detail, &items) == nil && len(items) > 0 {
			return items[0].Msg
		}
	}
	return ""
}

var (
	exceptionPrefix = regexp.MustCompile(`^(?:[A-Za-z_][\w]*\.)*[A-Z]\w*(?:Error|Exception|Warning):\s*`)
	anyURL          = regexp.MustCompile(`[A-Za-z][A-Za-z0-9+.-]*://[^\s"'<>]+`)
	internalHost    = regexp.MustCompile(`(?i)\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.(?:svc(?:\.cluster\.local)?|cluster\.local|internal|local)\b(?::\d+)?`)
	ipAddress       = regexp.MustCompile(`\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b`)
	sourceFile      = regexp.MustCompile(`[^\s"'()]*\.(?:py|rs|go|cc|cpp|cu|h)(?::\d+)?\b`)
	systemPath      = regexp.MustCompile(`(?:^|[\s"'(=])(/(?:usr|opt|home|root|app|workspace|tmp|var|etc|lib|lib64|srv|mnt|data|models|cache)(?:/[^\s"'()]*)?)`)
	whitespace      = regexp.MustCompile(`\s+`)
	// internalName is any token that names the stack behind this hop. The words are the
	// conformance suite's leak needles (libs/conformance/profiles/gateway.toml), so a quoted
	// message cannot fail the leak probe.
	internalName = regexp.MustCompile(`(?i)[^\s"'(),;]*(?:hangar|acme|partner|dynamo|vllm|ghcr\.io)[^\s"'(),;]*`)
)

// sanitizeUpstreamMessage makes an engine's message fit to put in front of a caller, or returns ""
// when nothing fit is left.
//
// It keeps the one line that says what was wrong and drops what says where: a traceback and
// everything after the first line, a leading exception class, URLs, cluster-internal hostnames, IP
// addresses, source files, filesystem paths, and any token naming the stack (dynamo, vllm, …).
// The served model name is written back as the name the caller asked for. The result is capped at
// maxQuotedUpstreamLen.
//
// This is a denylist over a string this hop does not author, and that is its stated cost: an engine
// that invents a new way to name its own infrastructure gets through until a pattern here names it.
// The full body is still logged, capped, at the level its class deserves.
func sanitizeUpstreamMessage(message string, name servedName) string {
	if i := strings.Index(message, "Traceback"); i >= 0 {
		message = message[:i]
	}
	if i := strings.IndexAny(message, "\r\n"); i >= 0 {
		message = message[:i]
	}
	message = strings.TrimSpace(message)
	for {
		stripped := exceptionPrefix.ReplaceAllString(message, "")
		if stripped == message {
			break
		}
		message = stripped
	}

	if name.served != "" && name.public != "" && name.served != name.public {
		message = strings.ReplaceAll(message, name.served, name.public)
	}
	message = anyURL.ReplaceAllString(message, redacted)
	message = internalHost.ReplaceAllString(message, redacted)
	message = ipAddress.ReplaceAllString(message, redacted)
	message = sourceFile.ReplaceAllString(message, redacted)
	message = internalName.ReplaceAllString(message, redacted)
	message = systemPath.ReplaceAllStringFunc(message, func(match string) string {
		lead := match[:strings.Index(match, "/")]
		return lead + redacted
	})
	message = strings.TrimSpace(whitespace.ReplaceAllString(message, " "))

	if len(message) > maxQuotedUpstreamLen {
		cut := maxQuotedUpstreamLen
		for cut > 0 && !utf8.RuneStart(message[cut]) {
			cut--
		}
		message = strings.TrimSpace(message[:cut]) + "…"
	}
	if strings.Trim(message, " .:;,-") == "" || message == redacted {
		return ""
	}
	return message
}
