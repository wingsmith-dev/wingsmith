package serving

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestNoPatternsIsNoPolicy(t *testing.T) {
	if NewMediaURLPolicy([]string{"", " "}) != nil {
		t.Fatal("empty patterns should build no policy")
	}
}

func TestTheMediaURLPolicyMatchesHostsAndSubdomains(t *testing.T) {
	p := NewMediaURLPolicy([]string{"media.example.com", "*.cdn.example.net"})
	for ref, want := range map[string]bool{
		"https://media.example.com/a.png":       true,
		"http://MEDIA.example.com/a.png":        true,
		"https://img.cdn.example.net/a.png":     true,
		"https://cdn.example.net/a.png":         false,
		"https://evil.example.com/a.png":        false,
		"http://169.254.169.254/latest/meta":    false,
		"file:///etc/passwd":                    false,
		"data:image/png;base64,iVBORw0KGgo=":    true,
		"https://media.example.com.evil.io/a":   false,
		"https://user@media.example.com:443/ok": true,
	} {
		if got := p.allows(ref); got != want {
			t.Errorf("allows(%q) = %v, want %v", ref, got, want)
		}
	}
}

func TestARequestWithADisallowedMediaURLIsRefused(t *testing.T) {
	handler := chatHandler(t, Config{MediaURLs: NewMediaURLPolicy([]string{"media.example.com"})})
	for _, tc := range []struct {
		name, body string
		status     int
	}{
		{"chat image allowed", `{"model":"chat","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://media.example.com/a.png"}}]}]}`, http.StatusOK},
		{"chat image refused", `{"model":"chat","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"http://10.0.0.1/a.png"}}]}]}`, http.StatusBadRequest},
		{"chat video refused", `{"model":"chat","messages":[{"role":"user","content":[{"type":"video_url","video_url":{"url":"http://internal.svc/v.mp4"}}]}]}`, http.StatusBadRequest},
		{"inline data passes", `{"model":"chat","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}}]}]}`, http.StatusOK},
		{"responses input_image refused", `{"model":"chat","input":[{"role":"user","content":[{"type":"input_image","image_url":"https://elsewhere.io/a.png"}]}]}`, http.StatusBadRequest},
		{"anthropic source refused", `{"model":"chat","messages":[{"role":"user","content":[{"type":"image","source":{"type":"url","url":"https://elsewhere.io/a.png"}}]}]}`, http.StatusBadRequest},
		{"a url in text is not media", `{"model":"chat","messages":[{"role":"user","content":"see https://elsewhere.io"}]}`, http.StatusOK},
		{"a url outside messages is not media", `{"model":"chat","tools":[{"type":"function","function":{"name":"f","parameters":{"url":"https://elsewhere.io"}}}],"messages":[]}`, http.StatusOK},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(tc.body)))
			if rec.Code != tc.status {
				t.Fatalf("status = %d, want %d: %s", rec.Code, tc.status, rec.Body)
			}
			if tc.status == http.StatusBadRequest && !strings.Contains(rec.Body.String(), "media_url_not_allowed") {
				t.Fatalf("body = %s, want media_url_not_allowed", rec.Body)
			}
		})
	}
}
