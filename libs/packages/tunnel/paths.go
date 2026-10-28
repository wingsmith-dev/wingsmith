package tunnel

import (
	"net/http"
	"sort"
)

// forwardedPaths are the API paths the gateway forwards to a serving runtime,
// over a tunnel or to a published address, all of them POST.
//
// Both ends refuse everything else. A serving runtime's frontend answers more
// than inference (metrics, health, admin and debug routes among them), and a
// path nobody listed here is not one a caller should reach through either hop.
// The gateway refuses before it reads the body; the agent refuses again before
// forwarding, so an agent never depends on a gateway build having checked. The
// list lives in this module because both ends have to agree on it.
var forwardedPaths = map[string]bool{
	"/v1/chat/completions":     true,
	"/v1/completions":          true,
	"/v1/responses":            true,
	"/v1/messages":             true,
	"/v1/embeddings":           true,
	"/v1/images/generations":   true,
	"/v1/images/edits":         true,
	"/v1/videos":               true,
	"/v1/videos/stream":        true,
	"/v1/audio/transcriptions": true,
	"/v1/audio/translations":   true,
	"/v1/audio/speech":         true,
}

// Forwarded reports whether a request with this method and path is one wingsmith
// forwards. The path is matched exactly, without its query.
func Forwarded(method, path string) bool {
	return method == http.MethodPost && forwardedPaths[path]
}

// ForwardedPaths lists the forwarded paths, sorted.
func ForwardedPaths() []string {
	paths := make([]string, 0, len(forwardedPaths))
	for path := range forwardedPaths {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	return paths
}
