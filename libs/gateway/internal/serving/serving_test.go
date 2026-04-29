package serving

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

type stubResolver struct {
	routes map[string]routing.Route
}

func (s stubResolver) Resolve(name string) (routing.Route, error) {
	route, ok := s.routes[name]
	if !ok {
		return routing.Route{}, routing.ErrUnknownEndpoint
	}
	return route, nil
}

func (s stubResolver) Names() []string {
	names := make([]string, 0, len(s.routes))
	for name := range s.routes {
		names = append(names, name)
	}
	return names
}

func (s stubResolver) Revision() string { return "1" }

func upstreamServer(t *testing.T) (*httptest.Server, *http.Request) {
	t.Helper()
	var seen *http.Request
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		r.Body = io.NopCloser(strings.NewReader(string(body)))
		seen = r
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	t.Cleanup(server.Close)
	return server, seen
}

func routeTo(t *testing.T, address string, headers http.Header) routing.Route {
	t.Helper()
	parsed, err := url.Parse(address)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return routing.NewRoute("chat", "weighted",
		routing.NewUpstream("ap", 100, true, NewReverseProxy(parsed, headers)))
}

func TestBodyIsForwardedVerbatim(t *testing.T) {
	server, _ := upstreamServer(t)
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, nil),
	}}})

	body := `{"model":"chat","messages":[{"role":"user","content":"hi"}]}`
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body)))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	if rec.Body.String() != body {
		t.Fatalf("body = %s, want it forwarded untouched", rec.Body.String())
	}
}

func TestUnknownModelIsNotFoundAndEmptyRouteIsCapacity(t *testing.T) {
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routing.NewRoute("chat", "weighted"),
	}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"nope"}`)))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown model = %d, want 404", rec.Code)
	}

	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"chat"}`)))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("no upstream = %d, want 503", rec.Code)
	}
}

func TestMalformedRequestsAreRejectedInTheOpenAIShape(t *testing.T) {
	handler := New(Config{Router: stubResolver{}})

	for _, tc := range []struct {
		name string
		body string
		code string
	}{
		{"not json", `{`, "invalid_json"},
		{"no model", `{"messages":[]}`, "missing_model"},
	} {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(tc.body)))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s: status = %d", tc.name, rec.Code)
		}
		var envelope errorEnvelope
		if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if envelope.Error.Code != tc.code {
			t.Fatalf("%s: code = %q", tc.name, envelope.Error.Code)
		}
	}
}

func TestModelListIsAnsweredLocally(t *testing.T) {
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": {}, "code": {},
	}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/models", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	var list modelList
	if err := json.Unmarshal(rec.Body.Bytes(), &list); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(list.Data) != 2 || list.Data[0].ID != "chat" {
		t.Fatalf("data = %v, want both models sorted", list.Data)
	}
}

func TestHealthzAnswersWithoutTheGuard(t *testing.T) {
	handler := New(Config{Router: stubResolver{}, Callers: NewTokenGuard("secret")})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz = %d, want it to answer with no token", rec.Code)
	}
}

func TestGuardRejectsCallersWithoutTheToken(t *testing.T) {
	handler := New(Config{Router: stubResolver{}, Callers: NewTokenGuard("secret")})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`)))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`))
	req.Header.Set("Authorization", "Bearer secret")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code == http.StatusUnauthorized {
		t.Fatal("the right token must be accepted")
	}
}

func TestAnEmptyTokenLeavesTheGatewayOpenRatherThanAcceptingEmptyStrings(t *testing.T) {
	if NewTokenGuard("") != nil {
		t.Fatal("an unset token must not build a guard")
	}
}

func TestCredentialHeadersAreStampedOnTheOutboundLegOnly(t *testing.T) {
	var seen http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	headers := http.Header{}
	headers.Set("X-Access-Token", "from-the-mount")
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, headers),
	}}})

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`))
	req.Header.Set("X-Access-Token", "from-the-caller")
	handler.ServeHTTP(httptest.NewRecorder(), req)

	if seen.Get("X-Access-Token") != "from-the-mount" {
		t.Fatalf("upstream saw %q, want the caller's value overwritten", seen.Get("X-Access-Token"))
	}
}

func TestModelIsRewrittenToTheNameTheEngineAnswersTo(t *testing.T) {
	server, _ := upstreamServer(t)
	route := routeTo(t, server.URL, nil)
	route.Upstreams[0].ServedModelName = "llama-3-3-70b-instruct-fp8-tp2-32k"
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{"chat": route}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"chat","messages":[{"role":"user","content":"hi"}],"stream":true}`)))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var forwarded map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &forwarded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if forwarded["model"] != "llama-3-3-70b-instruct-fp8-tp2-32k" {
		t.Fatalf("model = %v, want the served name", forwarded["model"])
	}
	if forwarded["stream"] != true {
		t.Fatalf("stream = %v, want every other field carried through", forwarded["stream"])
	}
	if _, ok := forwarded["messages"].([]any); !ok {
		t.Fatalf("messages = %v, want them intact", forwarded["messages"])
	}
}

func TestModelIsUntouchedWhenThePlaneNamesNoServedModel(t *testing.T) {
	server, _ := upstreamServer(t)
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, nil),
	}}})

	body := `{"model":"chat","messages":[{"role":"user","content":"hi"}]}`
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body)))

	if rec.Body.String() != body {
		t.Fatalf("body = %s, want it byte-identical when there is nothing to rewrite", rec.Body.String())
	}
}

func TestWithModelReplacesOnlyTheModel(t *testing.T) {
	body := []byte(`{"model":"chat","temperature":0.7,"tools":[{"type":"function"}],"n":2}`)
	out, err := withModel(body, "llama-tp2")
	if err != nil {
		t.Fatalf("withModel: %v", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(out, &fields); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if string(fields["model"]) != `"llama-tp2"` {
		t.Fatalf("model = %s", fields["model"])
	}
	for key, want := range map[string]string{
		"temperature": "0.7",
		"tools":       `[{"type":"function"}]`,
		"n":           "2",
	} {
		if string(fields[key]) != want {
			t.Fatalf("%s = %s, want %s untouched", key, fields[key], want)
		}
	}
}

func TestBothTokensAreAcceptedDuringARotation(t *testing.T) {
	guard := NewTokenGuard("old-secret,new-secret")
	for _, token := range []string{"old-secret", "new-secret"} {
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
		req.Header.Set("Authorization", "Bearer "+token)
		if !guard.Allow(req) {
			t.Fatalf("%s was refused; a rotation with no overlap is a coordinated flip", token)
		}
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	req.Header.Set("Authorization", "Bearer retired-secret")
	if guard.Allow(req) {
		t.Fatal("a token outside the set was accepted")
	}
}

func TestSeparatorsAndSpacingAreNotTokens(t *testing.T) {
	guard := NewTokenGuard(" old-secret , new-secret ,, ")
	for _, token := range []string{"old-secret", "new-secret"} {
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
		req.Header.Set("Authorization", "Bearer "+token)
		if !guard.Allow(req) {
			t.Fatalf("%s was refused; surrounding space must not be part of the secret", token)
		}
	}
	for _, empty := range []string{"", " "} {
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
		req.Header.Set("Authorization", "Bearer "+empty)
		if guard.Allow(req) {
			t.Fatalf("a trailing comma admitted %q as a token", empty)
		}
	}
	if NewTokenGuard(" , ,, ") != nil {
		t.Fatal("a value that is all separators must leave the gateway open, not admit nothing")
	}
}
