package serving

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestWithNoHostListOnlyInternalAndNonHTTPURLsAreRefused(t *testing.T) {
	p := NewMediaURLPolicy([]string{"", " "})
	for ref, want := range map[string]bool{
		"https://images.example.com/a.png":          true,
		"http://cdn.example.net:8080/a.png":         true,
		"https://8.8.8.8/a.png":                     true,
		"data:image/png;base64,iVBORw0KGgo=":        true,
		"http://169.254.169.254/latest/meta":        false,
		"http://127.0.0.1:8000/v1/models":           false,
		"http://[::1]/a":                            false,
		"http://[::ffff:10.0.0.1]/a":                false,
		"http://[fd00::1]/a":                        false,
		"http://10.1.2.3/a.png":                     false,
		"http://172.16.0.1/a.png":                   false,
		"http://192.168.1.1/a.png":                  false,
		"http://100.64.0.1/a.png":                   false,
		"http://0.0.0.0/a":                          false,
		"http://localhost/a":                        false,
		"http://wingsmith-plane/a":                  false,
		"http://plane.wingsmith.svc/a":              false,
		"http://plane.wingsmith.svc.cluster.local/": false,
		"http://metadata.google.internal/a":         false,
		"http://printer.local/a":                    false,
		"http://0x7f.1/a":                           false,
		"http://127.1/a":                            false,
		"http://2130706433/a":                       false,
		"http://good.example.com\\@10.0.0.1/a":      false,
		"file:///etc/passwd":                        false,
		"ftp://images.example.com/a.png":            false,
		"gopher://images.example.com/":              false,
		"http:images.example.com/a.png":             false,
	} {
		if got := p.allows(ref); got != want {
			t.Errorf("allows(%q) = %v, want %v", ref, got, want)
		}
	}
}

// A conversation's history carries URLs no engine fetches: citations and web
// search results. Refusing them would break a client replaying its own turns.
func TestURLsAnEngineDoesNotFetchAreNotChecked(t *testing.T) {
	p := NewMediaURLPolicy([]string{"media.example.com"})
	for name, body := range map[string]string{
		"responses url_citation": `{"input":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"x","annotations":[{"type":"url_citation","url":"https://news.example.org/a"}]}]}]}`,
		"anthropic web search":   `{"messages":[{"role":"assistant","content":[{"type":"web_search_tool_result","tool_use_id":"t","content":[{"type":"web_search_result","url":"https://news.example.org/a","title":"a"}]}]}]}`,
		"anthropic citation":     `{"messages":[{"role":"assistant","content":[{"type":"text","text":"x","citations":[{"type":"web_search_result_location","url":"https://news.example.org/a"}]}]}]}`,
		"chat text":              `{"messages":[{"role":"user","content":"https://news.example.org/a"}]}`,
		"tool schema":            `{"tools":[{"type":"function","function":{"name":"f","parameters":{"url":"http://10.0.0.1"}}}],"messages":[]}`,
		"base64 source":          `{"messages":[{"role":"user","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AAAA"}}]}]}`,
	} {
		if ref, refused := p.firstRefusedMediaURL("/v1/responses", []byte(body)); refused {
			t.Errorf("%s: refused %q, which no engine fetches", name, ref)
		}
	}
}

func TestEveryShapeAnEngineFetchesIsChecked(t *testing.T) {
	p := NewMediaURLPolicy(nil)
	for name, tc := range map[string]struct{ path, body string }{
		"chat image object":      {"/v1/chat/completions", `{"messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"http://10.0.0.1/a.png"}}]}]}`},
		"chat image string":      {"/v1/chat/completions", `{"messages":[{"role":"user","content":[{"type":"image_url","image_url":"http://10.0.0.1/a.png"}]}]}`},
		"chat audio":             {"/v1/chat/completions", `{"messages":[{"role":"user","content":[{"type":"audio_url","audio_url":{"url":"http://localhost/a.wav"}}]}]}`},
		"chat tool message":      {"/v1/chat/completions", `{"messages":[{"role":"tool","tool_call_id":"t","content":[{"type":"image_url","image_url":{"url":"http://10.0.0.1/a.png"}}]}]}`},
		"responses input_image":  {"/v1/responses", `{"input":[{"role":"user","content":[{"type":"input_image","image_url":"http://10.0.0.1/a.png"}]}]}`},
		"responses input_file":   {"/v1/responses", `{"input":[{"type":"message","role":"user","content":[{"type":"input_file","file_url":"file:///etc/passwd"}]}]}`},
		"responses tool output":  {"/v1/responses", `{"input":[{"type":"function_call_output","call_id":"c","output":[{"type":"input_image","image_url":"http://10.0.0.1/a.png"}]}]}`},
		"anthropic image":        {"/v1/messages", `{"messages":[{"role":"user","content":[{"type":"image","source":{"type":"url","url":"http://10.0.0.1/a.png"}}]}]}`},
		"anthropic document":     {"/v1/messages", `{"messages":[{"role":"user","content":[{"type":"document","source":{"type":"url","url":"http://10.0.0.1/a.pdf"}}]}]}`},
		"anthropic tool result":  {"/v1/messages", `{"messages":[{"role":"user","content":[{"type":"tool_result","tool_use_id":"t","content":[{"type":"image","source":{"type":"url","url":"http://10.0.0.1/a.png"}}]}]}]}`},
		"image edit field":       {"/v1/images/edits", `{"model":"m","prompt":"a cat","image":"http://10.0.0.1/a.png"}`},
		"image edit nested":      {"/v1/images/edits", `{"model":"m","prompt":"a cat","images":[{"image_url":"http://10.0.0.1/a.png"}],"mask":{"image_url":"data:image/png;base64,AAAA"}}`},
		"video input reference":  {"/v1/videos", `{"model":"m","prompt":"a cat","input_reference":"http://169.254.169.254/latest"}`},
		"video stream reference": {"/v1/videos/stream", `{"model":"m","prompt":"x","nvext":{"image":"http://localhost/a.png"}}`},
	} {
		if _, refused := p.firstRefusedMediaURL(tc.path, []byte(tc.body)); !refused {
			t.Errorf("%s: an internal media URL was let through", name)
		}
	}
}

func TestAGenerationPromptOrIdIsNotTreatedAsAURL(t *testing.T) {
	p := NewMediaURLPolicy([]string{"media.example.com"})
	body := `{"model":"org/model","prompt":"redraw http://10.0.0.1 as a cat","user":"tenant:42","size":"1024x1024","image":"https://media.example.com/a.png"}`
	if ref, refused := p.firstRefusedMediaURL("/v1/images/edits", []byte(body)); refused {
		t.Fatalf("refused %q", ref)
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
