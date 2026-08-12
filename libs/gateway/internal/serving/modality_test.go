package serving

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

func TestPathAnswered(t *testing.T) {
	cases := []struct {
		modality, path string
		want           bool
	}{
		{"", "/v1/chat/completions", true},
		{"text", "/v1/embeddings", true},
		{"text", "/v1/images/generations", false},
		{"text", "/v1/videos", false},
		{"image", "/v1/images/generations", true},
		{"image", "/v1/images/edits", true},
		{"image", "/v1/chat/completions", true},
		{"image", "/v1/completions", false},
		{"image", "/v1/embeddings", false},
		{"image", "/v1/messages", false},
		{"image", "/v1/videos", false},
		{"video", "/v1/videos", true},
		{"video", "/v1/chat/completions", false},
		{"video", "/v1/images/generations", false},
		{"audio", "/v1/audio/speech", true},
	}
	for _, tc := range cases {
		if got := pathAnswered(tc.modality, tc.path); got != tc.want {
			t.Errorf("pathAnswered(%q, %q) = %v, want %v", tc.modality, tc.path, got, tc.want)
		}
	}
}

func modalityRoute(t *testing.T, name, address, modality string) routing.Route {
	t.Helper()
	parsed, err := url.Parse(address)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	upstream := routing.NewUpstream("ap", 100, true, NewReverseProxy(parsed, nil, 0))
	upstream.OutputModality = modality
	return routing.NewRoute(name, "weighted", upstream)
}

func TestAPathTheUpstreamDoesNotAnswerIsRefused(t *testing.T) {
	server, _ := upstreamServer(t)
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat":  modalityRoute(t, "chat", server.URL, ""),
		"image": modalityRoute(t, "image", server.URL, "image"),
	}}})

	for _, tc := range []struct {
		path, model string
		want        int
	}{
		{"/v1/images/generations", "chat", http.StatusNotFound},
		{"/v1/chat/completions", "chat", http.StatusOK},
		{"/v1/images/generations", "image", http.StatusOK},
		{"/v1/chat/completions", "image", http.StatusOK},
		{"/v1/embeddings", "image", http.StatusNotFound},
	} {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, tc.path,
			strings.NewReader(`{"model":"`+tc.model+`","prompt":"a fox"}`)))
		if rec.Code != tc.want {
			t.Errorf("%s to %s = %d, want %d: %s", tc.path, tc.model, rec.Code, tc.want, rec.Body.String())
		}
		if tc.want == http.StatusNotFound && !strings.Contains(rec.Body.String(), "unsupported_path") {
			t.Errorf("%s to %s: body = %s, want an unsupported_path error", tc.path, tc.model, rec.Body.String())
		}
	}
}

func TestTheCatalogReportsWhatEachModelGenerates(t *testing.T) {
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat":  modalityRoute(t, "chat", "http://127.0.0.1:1", ""),
		"image": modalityRoute(t, "image", "http://127.0.0.1:1", "image"),
	}}})
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/models", nil))

	var list modelList
	if err := json.Unmarshal(rec.Body.Bytes(), &list); err != nil {
		t.Fatalf("decode: %v: %s", err, rec.Body.String())
	}
	got := map[string]string{}
	for _, m := range list.Data {
		got[m.ID] = m.OutputModality
	}
	if got["chat"] != "text" || got["image"] != "image" {
		t.Fatalf("catalog kinds = %v", got)
	}
}
