package gateway

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
