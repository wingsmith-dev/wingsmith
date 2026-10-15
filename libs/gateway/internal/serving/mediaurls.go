package serving

import (
	"encoding/json"
	"net/http"
	"net/netip"
	"net/url"
	"strings"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
)

// MediaURLPolicy restricts the remote media a request may ask an engine to
// fetch.
//
// A multimodal request can carry a URL instead of bytes, and the engine fetches
// it from inside the serving cluster: from a GPU node, on the cluster's
// network, with whatever that network can reach. So every remote media URL is
// checked before the request leaves this hop:
//
//   - `data:` URLs carry bytes and always pass.
//   - Anything that is not `http` or `https` is refused (`file:` above all).
//   - With no host list, a URL is refused when its host is an address or a name
//     that only means something inside a cluster or a private network:
//     loopback, private, link-local (the cloud metadata address among them),
//     CGNAT and unspecified addresses, numeric host forms, single-label names
//     (which the cluster's DNS search path completes), and the `.local`,
//     `.localhost`, `.internal`, `.svc`, `.cluster.local`, `.lan` and
//     `.home.arpa` suffixes. Every other host passes.
//   - With a host list (HANGAR_GATEWAY_MEDIA_URL_HOSTS), a URL must name a host
//     on it, and nothing else passes. A listed private host is allowed: listing
//     it is the operator's decision.
//
// The check is by name. A public name that resolves to a private address is not
// caught here, which is one reason it does not replace the engine's own setting
// (vLLM's --allowed-media-domains) or an egress NetworkPolicy on the worker
// namespace. Use them together.
type MediaURLPolicy struct {
	exact    map[string]bool
	suffixes []string
}

// NewMediaURLPolicy builds a policy from host patterns: `example.com` matches
// that host, `*.example.com` matches any subdomain of it. With no patterns the
// policy refuses only internal and non-http(s) URLs.
func NewMediaURLPolicy(patterns []string) *MediaURLPolicy {
	p := &MediaURLPolicy{exact: map[string]bool{}}
	for _, raw := range patterns {
		pattern := strings.ToLower(strings.TrimSpace(raw))
		switch {
		case pattern == "":
		case strings.HasPrefix(pattern, "*."):
			p.suffixes = append(p.suffixes, pattern[1:])
		default:
			p.exact[pattern] = true
		}
	}
	return p
}

// listed reports whether the policy carries a host list.
func (p *MediaURLPolicy) listed() bool {
	return len(p.exact) > 0 || len(p.suffixes) > 0
}

// allows reports whether a media reference may be forwarded. A nil policy
// allows everything; the binary always builds one.
func (p *MediaURLPolicy) allows(ref string) bool {
	if p == nil || strings.HasPrefix(ref, "data:") {
		return true
	}
	// A backslash, whitespace or control character is where URL parsers
	// disagree, and the engine's parser is not this one.
	if strings.ContainsAny(ref, "\\ \t\r\n") || strings.ContainsFunc(ref, func(r rune) bool { return r < 0x20 || r == 0x7f }) {
		return false
	}
	parsed, err := url.Parse(ref)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Opaque != "" {
		return false
	}
	host := strings.TrimSuffix(strings.ToLower(parsed.Hostname()), ".")
	if host == "" {
		return false
	}
	if !p.listed() {
		return !privateMediaHost(host)
	}
	if p.exact[host] {
		return true
	}
	for _, suffix := range p.suffixes {
		if strings.HasSuffix(host, suffix) {
			return true
		}
	}
	return false
}

// cgnat is 100.64.0.0/10, which many clusters use for pod and node addresses.
var cgnat = netip.MustParsePrefix("100.64.0.0/10")

// internalSuffixes are DNS suffixes that only resolve inside a cluster or a
// private network.
var internalSuffixes = []string{".local", ".localhost", ".internal", ".svc", ".cluster.local", ".lan", ".home.arpa"}

// privateMediaHost reports whether a lowercased host names something that is not a
// public internet address.
func privateMediaHost(host string) bool {
	if addr, err := netip.ParseAddr(host); err == nil {
		addr = addr.Unmap()
		return !addr.IsGlobalUnicast() || addr.IsPrivate() || cgnat.Contains(addr)
	}
	if host == "localhost" || !strings.Contains(host, ".") {
		return true
	}
	for _, suffix := range internalSuffixes {
		if strings.HasSuffix(host, suffix) {
			return true
		}
	}
	// No top-level domain is numeric, so a host ending in a number is an
	// address in a form netip does not read (`0x7f.1`, `127.1`, `017700000001`)
	// that a resolver's inet_aton may still accept.
	last := host[strings.LastIndexByte(host, '.')+1:]
	return last == "" || strings.HasPrefix(last, "0x") || strings.Trim(last, "0123456789") == ""
}

// maxMediaDepth bounds how deep content parts may nest (a tool result inside a
// message, a message inside a Responses input) before the walk stops looking.
const maxMediaDepth = 8

// mediaPart is one content part, decoded only as far as the walk needs. Fields
// it does not name, the text of a text part among them, are skipped by the
// decoder without being copied.
type mediaPart struct {
	Type     string          `json:"type"`
	ImageURL json.RawMessage `json:"image_url"`
	VideoURL json.RawMessage `json:"video_url"`
	AudioURL json.RawMessage `json:"audio_url"`
	FileURL  json.RawMessage `json:"file_url"`
	Source   json.RawMessage `json:"source"`
	// Content holds nested parts: a message's, or an Anthropic tool result's.
	Content json.RawMessage `json:"content"`
	// Output holds a Responses function_call_output's parts.
	Output json.RawMessage `json:"output"`
}

// mediaRefs returns every remote media reference a request asks an engine to
// fetch.
//
// Only the places an engine fetches from, in the shapes it reads them:
//
//   - a chat part `image_url`, `video_url` or `audio_url`, as `{"url": ...}`
//     or a bare string;
//   - a Responses `input_image.image_url` or `input_file.file_url`;
//   - an Anthropic `image` or `document` block whose `source.type` is `url`;
//   - those same parts nested in a message's, a tool result's or a function
//     call output's content;
//   - on the image edit and video paths, the request's own top-level fields
//     (see topLevelMediaRefs).
//
// A `url` anywhere else is not fetched by anything: a citation, a web search
// result or a tool schema in a conversation's history is text, and refusing it
// would break a Responses or Messages client replaying its own history.
//
// `messages` and `input` are found by scanning the body's top-level keys and
// walked in place, so a long conversation is not copied to be checked. Every
// occurrence is walked when a key is repeated, since engines differ on which
// one wins.
func mediaRefs(path string, body []byte) []string {
	var refs []string
	for _, field := range []string{"messages", "input"} {
		_, spans, ok := topLevelValues(body, field)
		if !ok {
			// Structure the scan does not take on: decode instead of skipping
			// the check.
			var fields map[string]json.RawMessage
			if json.Unmarshal(body, &fields) == nil {
				refs = appendPartRefs(refs, fields[field], 0)
			}
			continue
		}
		for _, at := range spans {
			refs = appendPartRefs(refs, body[at.start:at.end], 0)
		}
	}
	if topLevelMediaPaths[path] {
		var fields map[string]json.RawMessage
		if json.Unmarshal(body, &fields) == nil {
			refs = append(refs, topLevelMediaRefs(fields)...)
		}
	}
	return refs
}

// appendPartRefs walks an array of messages or parts. Anything that is not an
// array (a string prompt, a missing field) carries no parts.
func appendPartRefs(refs []string, raw json.RawMessage, depth int) []string {
	if depth >= maxMediaDepth {
		return refs
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil {
		return refs
	}
	for _, item := range items {
		var part mediaPart
		if json.Unmarshal(item, &part) != nil {
			continue
		}
		switch part.Type {
		case "image_url":
			refs = appendRef(refs, part.ImageURL)
		case "video_url":
			refs = appendRef(refs, part.VideoURL)
		case "audio_url":
			refs = appendRef(refs, part.AudioURL)
		case "input_image":
			refs = appendRef(refs, part.ImageURL)
		case "input_file":
			refs = appendRef(refs, part.FileURL)
		case "image", "document":
			var source struct {
				Type string `json:"type"`
				URL  string `json:"url"`
			}
			if json.Unmarshal(part.Source, &source) == nil && source.Type == "url" {
				refs = append(refs, source.URL)
			}
		default:
			refs = appendPartRefs(refs, part.Content, depth+1)
			if part.Type == "function_call_output" {
				refs = appendPartRefs(refs, part.Output, depth+1)
			}
		}
	}
	return refs
}

// appendRef reads a reference written either as a string or as `{"url": ...}`.
func appendRef(refs []string, raw json.RawMessage) []string {
	var ref string
	if json.Unmarshal(raw, &ref) == nil {
		if ref != "" {
			refs = append(refs, ref)
		}
		return refs
	}
	var wrapped struct {
		URL string `json:"url"`
	}
	if json.Unmarshal(raw, &wrapped) == nil && wrapped.URL != "" {
		refs = append(refs, wrapped.URL)
	}
	return refs
}

// topLevelMediaPaths carry their input media as top-level request fields rather
// than as content parts.
var topLevelMediaPaths = map[string]bool{
	"/v1/images/edits":  true,
	"/v1/videos":        true,
	"/v1/videos/stream": true,
}

// promptFields are top-level fields of a generation request that hold the
// caller's text, never a reference.
var promptFields = map[string]bool{"prompt": true, "negative_prompt": true}

// topLevelMediaRefs returns every string in a generation request, outside its
// prompt, that is written as a URL. These requests are small and their schemas
// differ by engine (`image`, `images[].image_url`, `mask`, `input_reference`
// and more), so any absolute URL in them is treated as one the engine may
// fetch rather than trusting a field list to be complete.
func topLevelMediaRefs(fields map[string]json.RawMessage) []string {
	var refs []string
	for key, raw := range fields {
		if promptFields[key] || key == "messages" || key == "input" {
			continue
		}
		var tree any
		if json.Unmarshal(raw, &tree) != nil {
			continue
		}
		refs = appendURLStrings(refs, tree, 0)
	}
	return refs
}

func appendURLStrings(refs []string, node any, depth int) []string {
	if depth >= maxMediaDepth {
		return refs
	}
	switch value := node.(type) {
	case string:
		if looksLikeURL(value) {
			refs = append(refs, value)
		}
	case map[string]any:
		for _, child := range value {
			refs = appendURLStrings(refs, child, depth+1)
		}
	case []any:
		for _, child := range value {
			refs = appendURLStrings(refs, child, depth+1)
		}
	}
	return refs
}

// looksLikeURL is a string that starts with a scheme followed by `//`, or a
// `file:` or `data:` reference. Base64 bytes, plain words and ids such as
// `tenant:42` do not.
func looksLikeURL(s string) bool {
	colon := strings.IndexByte(s, ':')
	if colon < 1 || colon > 16 {
		return false
	}
	scheme := strings.ToLower(s[:colon])
	for _, r := range scheme {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '+' || r == '-' || r == '.') {
			return false
		}
	}
	return scheme == "file" || scheme == "data" || strings.HasPrefix(s[colon+1:], "//")
}

// firstRefusedMediaURL returns the first media reference in a request the
// policy refuses.
func (p *MediaURLPolicy) firstRefusedMediaURL(path string, body []byte) (string, bool) {
	for _, ref := range mediaRefs(path, body) {
		if !p.allows(ref) {
			return ref, true
		}
	}
	return "", false
}

// stageMediaURLs refuses a request carrying a media URL the policy does not
// allow. A multipart body carries its media as bytes and is not walked.
func stageMediaURLs(policy *MediaURLPolicy) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if policy == nil || req.isMultipart || len(req.body) == 0 {
			return r, true
		}
		if _, refused := policy.firstRefusedMediaURL(r.URL.Path, req.body); refused {
			MarkOutcome(r, metrics.OutcomeClientError)
			message := "A media URL in this request points at a private or cluster-internal address, or is not http or https. Send the media inline as a data URL instead."
			if policy.listed() {
				message = "A media URL in this request is not on this gateway's list of allowed hosts. Send the media inline as a data URL instead."
			}
			writeError(w, http.StatusBadRequest, "media_url_not_allowed", message)
			return r, false
		}
		return r, true
	}
}
