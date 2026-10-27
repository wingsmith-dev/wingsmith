package tunnel

import (
	"net/http"
	"testing"
)

func TestOnlyAPostToAListedPathIsForwarded(t *testing.T) {
	for _, tc := range []struct {
		method, path string
		want         bool
	}{
		{http.MethodPost, "/v1/chat/completions", true},
		{http.MethodPost, "/v1/messages", true},
		{http.MethodPost, "/v1/audio/speech", true},
		{http.MethodGet, "/v1/chat/completions", false},
		{http.MethodGet, "/v1/models", false},
		{http.MethodGet, "/metrics", false},
		{http.MethodPost, "/tokenize", false},
		{http.MethodPost, "/v1/chat/completions/", false},
		{http.MethodPost, "/v1/videos/abc/content", false},
		{http.MethodDelete, "/v1/videos", false},
	} {
		if got := Forwarded(tc.method, tc.path); got != tc.want {
			t.Errorf("Forwarded(%s, %s) = %v, want %v", tc.method, tc.path, got, tc.want)
		}
	}
	if got := len(ForwardedPaths()); got != len(forwardedPaths) {
		t.Errorf("ForwardedPaths lists %d paths, want %d", got, len(forwardedPaths))
	}
}
