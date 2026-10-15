package serving

import (
	"encoding/json"
	"net/http"
	"net/url"
	"strings"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
)

// MediaURLPolicy restricts the remote media a request may ask an engine to
// fetch.
//
// A multimodal request can carry a URL instead of bytes, and the engine fetches
// it from inside the serving cluster: from a GPU node, on the cluster's
// network, with whatever that network can reach. Unrestricted, any tenant can
// make a worker fetch an internal address. With a policy, a remote URL must be
// http or https and its host must be on the list; anything else is refused
// before the request leaves this hop. `data:` URLs carry bytes and always pass.
//
// It is a gateway-side check and not a substitute for the engine's own
// setting (vLLM's --allowed-media-domains) or an egress NetworkPolicy on the
// worker namespace. Use them together.
type MediaURLPolicy struct {
	exact    map[string]bool
	suffixes []string
}

// NewMediaURLPolicy builds a policy from host patterns: `example.com` matches
// that host, `*.example.com` matches any subdomain of it. No patterns returns
// nil, which allows every URL.
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
	if len(p.exact) == 0 && len(p.suffixes) == 0 {
		return nil
	}
	return p
}

// allows reports whether a media reference may be forwarded.
func (p *MediaURLPolicy) allows(ref string) bool {
	if p == nil || strings.HasPrefix(ref, "data:") {
		return true
	}
	parsed, err := url.Parse(ref)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return false
	}
	host := strings.ToLower(parsed.Hostname())
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

// mediaURLKeys are the keys whose string value is a media reference, in the
// OpenAI chat (`image_url.url`, `video_url.url`, `audio_url.url`), Responses
// (`input_image.image_url`) and Anthropic (`source.url`) shapes alike.
var mediaURLKeys = map[string]bool{
	"url":       true,
	"image_url": true,
	"video_url": true,
	"audio_url": true,
	"file_url":  true,
}

// firstRefusedMediaURL walks `messages` and `input` for a media reference the
// policy refuses, and returns it. Only those two fields: they are where every
// supported API carries content parts, and a `url` anywhere else in a body
// (a tool's schema, say) is not something an engine fetches.
func (p *MediaURLPolicy) firstRefusedMediaURL(body []byte) (string, bool) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		return "", false
	}
	for _, field := range []string{"messages", "input"} {
		raw, ok := fields[field]
		if !ok {
			continue
		}
		var tree any
		if err := json.Unmarshal(raw, &tree); err != nil {
			continue
		}
		if ref, refused := p.walk(tree); refused {
			return ref, true
		}
	}
	return "", false
}

func (p *MediaURLPolicy) walk(node any) (string, bool) {
	switch value := node.(type) {
	case map[string]any:
		for key, child := range value {
			if ref, isString := child.(string); isString && mediaURLKeys[key] && strings.Contains(ref, ":") {
				if !p.allows(ref) {
					return ref, true
				}
				continue
			}
			if ref, refused := p.walk(child); refused {
				return ref, true
			}
		}
	case []any:
		for _, child := range value {
			if ref, refused := p.walk(child); refused {
				return ref, true
			}
		}
	}
	return "", false
}

// stageMediaURLs refuses a request carrying a media URL the policy does not
// allow. Free when no policy is configured; with one, it decodes `messages`
// and `input` once per request.
func stageMediaURLs(policy *MediaURLPolicy) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if policy == nil || req.isMultipart || len(req.body) == 0 {
			return r, true
		}
		if _, refused := policy.firstRefusedMediaURL(req.body); refused {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusBadRequest, "media_url_not_allowed",
				"A media URL in this request is not on this gateway's list of allowed hosts. Send the media inline as a data URL instead.")
			return r, false
		}
		return r, true
	}
}
