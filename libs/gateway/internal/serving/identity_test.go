package serving

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/routing"
)

// accessLines runs a handler with slog captured and returns the access lines it
// wrote, decoded. Captured through the default logger instead of an injected
// one because that is how the access log is written, and a test that injected a
// logger would not be exercising the same call.
func accessLines(t *testing.T, serve func()) []map[string]any {
	t.Helper()
	var buf bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, nil)))
	defer slog.SetDefault(previous)

	serve()

	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		if line == "" {
			continue
		}
		var fields map[string]any
		if err := json.Unmarshal([]byte(line), &fields); err != nil {
			t.Fatalf("decode log line %q: %v", line, err)
		}
		if fields["msg"] == "gateway: request" {
			out = append(out, fields)
		}
	}
	return out
}

func TestTheAccessLogNamesTheTenantAndKeyThatSpokeForARequest(t *testing.T) {
	server, _ := upstreamServer(t)
	tenants := tenantsFor(t, globex())
	handler := publicHandler(t, map[string]routing.Route{"chat": globexRoute(t, server.URL)}, tenants)

	lines := accessLines(t, func() {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, requestTo("globex.wingsmith.dev", `{"model":"chat"}`, "Authorization", "Bearer secret-key"))
		if rec.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200", rec.Code)
		}
	})

	if len(lines) != 1 {
		t.Fatalf("access lines = %d, want 1", len(lines))
	}
	line := lines[0]
	if line["tenant"] != "globex" {
		t.Errorf("tenant = %v, want globex", line["tenant"])
	}
	if line["tenant_id"] != "ten_globex" {
		t.Errorf("tenant_id = %v, want ten_globex", line["tenant_id"])
	}

	// The prefix of the digest, and never the key: a line carrying
	// "secret-key" would put a live credential in the log stream.
	want := keyHash("secret-key")[:keyPrefixLen]
	if line["key"] != want {
		t.Errorf("key = %v, want %v", line["key"], want)
	}
	if strings.Contains(strings.ToLower(line["key"].(string)), "secret-key") {
		t.Error("the access log carried the presented key itself")
	}
}

// A rejected key is the one an on-call most needs attributed, and it is
// recorded before the lookup that rejects it.
func TestTheAccessLogNamesTheKeyOnARejectedRequest(t *testing.T) {
	tenants := tenantsFor(t, globex())
	handler := publicHandler(t, nil, tenants)

	lines := accessLines(t, func() {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, requestTo("globex.wingsmith.dev", `{"model":"chat"}`, "Authorization", "Bearer wrong-key"))
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("status = %d, want 401", rec.Code)
		}
	})

	if len(lines) != 1 {
		t.Fatalf("access lines = %d, want 1", len(lines))
	}
	if got, want := lines[0]["key"], keyHash("wrong-key")[:keyPrefixLen]; got != want {
		t.Errorf("key = %v, want %v", got, want)
	}
	if lines[0]["outcome"] != "auth_rejected" {
		t.Errorf("outcome = %v, want auth_rejected", lines[0]["outcome"])
	}
}

// The internal zone resolves no tenant, so the fields are absent, not
// empty: a line saying tenant="" would aggregate with every other one in a
// query that grouped by it.
func TestTheAccessLogCarriesNoIdentityOnTheInternalZone(t *testing.T) {
	server, _ := upstreamServer(t)
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{"chat": routeTo(t, server.URL, nil)}}})

	lines := accessLines(t, func() {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, requestTo("anything.wingsmith.dev", `{"model":"chat"}`))
		if rec.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200", rec.Code)
		}
	})

	if len(lines) != 1 {
		t.Fatalf("access lines = %d, want 1", len(lines))
	}
	for _, absent := range []string{"tenant", "tenant_id", "key"} {
		if _, ok := lines[0][absent]; ok {
			t.Errorf("%s present on an internal-zone line, want absent", absent)
		}
	}
}
