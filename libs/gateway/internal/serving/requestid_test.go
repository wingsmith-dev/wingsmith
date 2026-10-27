package serving

import (
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

var uuidV4 = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

// TestEveryResponseCarriesTheIdTheAccessLogAndTheClusterSaw is the correlation support depends on:
// the id a caller reads off the response is the id on the gateway's line and the id Dynamo logged.
func TestEveryResponseCarriesTheIdTheAccessLogAndTheClusterSaw(t *testing.T) {
	server, seen := steeringUpstream(t)
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, nil),
	}}})

	rec := httptest.NewRecorder()
	lines := accessLines(t, func() {
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
			strings.NewReader(`{"model":"chat"}`)))
	})

	id := rec.Header().Get("X-Request-Id")
	if !uuidV4.MatchString(id) {
		t.Fatalf("X-Request-Id = %q, want a minted UUIDv4", id)
	}
	if got := seen.header.Get("X-Request-Id"); got != id {
		t.Fatalf("the cluster saw %q, the caller was told %q", got, id)
	}
	if len(lines) != 1 || lines[0]["request_id"] != id {
		t.Fatalf("access lines = %v, want one carrying request_id=%s", lines, id)
	}
}

// TestACallersOwnRequestIdIsHonouredWhenItIsSane because it is what their own logs already carry.
func TestACallersOwnRequestIdIsHonouredWhenItIsSane(t *testing.T) {
	for _, test := range []struct {
		sent string
		kept bool
	}{
		{"trace-7f3c", true},
		{"01J8ZK3Q7M:req.42_a", true},
		{"has space", false},
		{"line\nbreak", false},
		{`"quoted"`, false},
		{strings.Repeat("a", maxRequestIDLen+1), false},
	} {
		server, seen := steeringUpstream(t)
		handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
			"chat": routeTo(t, server.URL, nil),
		}}})
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`))
		req.Header["X-Request-Id"] = []string{test.sent}
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)

		got := rec.Header().Get("X-Request-Id")
		if test.kept && got != test.sent {
			t.Errorf("sent %q, answered %q, want it kept", test.sent, got)
		}
		if !test.kept && !uuidV4.MatchString(got) {
			t.Errorf("sent %q, answered %q, want a fresh id in its place", test.sent, got)
		}
		if seen.header.Get("X-Request-Id") != got {
			t.Errorf("sent %q, the cluster saw %q and the caller %q", test.sent, seen.header.Get("X-Request-Id"), got)
		}
	}
}

// TestAnErrorThisHopWritesItselfCarriesTheId covers the answers that never reach a proxy: a
// refused body, an unknown model, a cluster that never answered.
func TestAnErrorThisHopWritesItselfCarriesTheId(t *testing.T) {
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, "http://127.0.0.1:1", nil),
	}}})
	for _, body := range []string{`{`, `{"model":"nope"}`, `{"model":"chat"}`} {
		rec := httptest.NewRecorder()
		lines := accessLines(t, func() {
			handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
				strings.NewReader(body)))
		})
		if rec.Code < 400 {
			t.Fatalf("%s: status = %d, want an error", body, rec.Code)
		}
		id := rec.Header().Get("X-Request-Id")
		if id == "" || len(lines) != 1 || lines[0]["request_id"] != id {
			t.Fatalf("%s: X-Request-Id = %q, lines = %v", body, id, lines)
		}
	}
}
