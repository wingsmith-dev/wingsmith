package serving

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	"github.com/hangar-dev/hangar/libs/gateway/internal/tenancy"
)

type fixedTenantSource struct {
	table plane.TenantTable
}

func (s fixedTenantSource) TenantsResolved(_ context.Context, _ string) (plane.TenantTable, error) {
	return s.table, nil
}

// tenantsFor builds a loaded *tenancy.Store the same way production does —
// through Load against a Source — rather than reaching into the package for
// a shortcut it does not export. A test fixture that skipped Load could drift
// from what a real refresh actually produces.
func tenantsFor(t *testing.T, tenants ...plane.GatewayTenant) *tenancy.Store {
	t.Helper()
	store := tenancy.New(fixedTenantSource{table: plane.TenantTable{Revision: "1", Tenants: tenants}})
	if err := store.Load(context.Background()); err != nil {
		t.Fatalf("load tenants: %v", err)
	}
	return store
}

func globex() plane.GatewayTenant {
	return plane.GatewayTenant{
		ID:        "ten_globex",
		Slug:      "globex",
		Enabled:   true,
		Ingress:   "public",
		Auth:      "api_key",
		Host:      "globex.hangar.dev",
		KeyHashes: []string{keyHash("secret-key")},
	}
}

func publicHandler(t *testing.T, extraRoutes map[string]routing.Route, tenants *tenancy.Store) http.Handler {
	t.Helper()
	routes := map[string]routing.Route{}
	for name, route := range extraRoutes {
		routes[name] = route
	}
	return New(Config{
		Router:  stubResolver{routes: routes},
		Tenants: tenants,
		Zone:    ZonePublic,
	})
}

func globexRoute(t *testing.T, address string) routing.Route {
	t.Helper()
	route := routeTo(t, address, nil)
	route.TenantID = "ten_globex"
	return route
}

func requestTo(host, body string, headers ...string) *http.Request {
	r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	r.Host = host
	for i := 0; i+1 < len(headers); i += 2 {
		r.Header.Set(headers[i], headers[i+1])
	}
	return r
}

func TestAHostThatNamesNoTenantIsRefusedOnThePublicZone(t *testing.T) {
	tenants := tenantsFor(t, globex())
	handler := publicHandler(t, nil, tenants)

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("nobody.hangar.dev", `{"model":"chat"}`))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
}

func TestAKeyedTenantWithNoKeyPresentedIsUnauthorized(t *testing.T) {
	tenants := tenantsFor(t, globex())
	handler := publicHandler(t, nil, tenants)

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("globex.hangar.dev", `{"model":"chat"}`))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
}

func TestAKeyBelongingToAnotherTenantIsUnauthorized(t *testing.T) {
	other := plane.GatewayTenant{
		ID: "ten_other", Slug: "other", Enabled: true,
		Ingress: "public", Auth: "api_key", Host: "other.hangar.dev",
		KeyHashes: []string{keyHash("other-secret")},
	}
	tenants := tenantsFor(t, globex(), other)
	handler := publicHandler(t, nil, tenants)

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("globex.hangar.dev", `{"model":"chat"}`, "Authorization", "Bearer other-secret"))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401 — a key minted for a different tenant must not pass", rec.Code)
	}
}

func TestTheRightKeyForTheRightTenantIsAccepted(t *testing.T) {
	server, _ := upstreamServer(t)
	tenants := tenantsFor(t, globex())
	handler := publicHandler(t, map[string]routing.Route{"chat": globexRoute(t, server.URL)}, tenants)

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("globex.hangar.dev", `{"model":"chat"}`, "Authorization", "Bearer secret-key"))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s, want 200", rec.Code, rec.Body.String())
	}
}

func TestAnotherTenantsModelIsNotFoundNotForbidden(t *testing.T) {
	server, _ := upstreamServer(t)
	tenants := tenantsFor(t, globex())
	acmesChat := routeTo(t, server.URL, nil)
	acmesChat.TenantID = "ten_acme"
	handler := publicHandler(t, map[string]routing.Route{"chat": acmesChat}, tenants)

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("globex.hangar.dev", `{"model":"chat"}`, "Authorization", "Bearer secret-key"))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404 — a name resolving to another tenant's endpoint must read as absent", rec.Code)
	}
}

func TestTheCatalogOnThePublicZoneListsOnlyThisTenantsModels(t *testing.T) {
	server, _ := upstreamServer(t)
	tenants := tenantsFor(t, globex())
	acmesChat := routeTo(t, server.URL, nil)
	acmesChat.TenantID = "ten_acme"
	handler := publicHandler(t, map[string]routing.Route{
		"chat":       globexRoute(t, server.URL),
		"acme-x": acmesChat,
	}, tenants)

	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Host = "globex.hangar.dev"
	req.Header.Set("Authorization", "Bearer secret-key")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if strings.Contains(rec.Body.String(), "acme-x") {
		t.Fatalf("catalog leaked another tenant's model: %s", rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "chat") {
		t.Fatalf("catalog missing this tenant's own model: %s", rec.Body.String())
	}
}

func TestATenantDeclaringNoAuthIsNotAskedForAKey(t *testing.T) {
	server, _ := upstreamServer(t)
	open := plane.GatewayTenant{
		ID: "ten_open", Slug: "open", Enabled: true,
		Ingress: "public", Auth: "none", Host: "open.hangar.dev",
	}
	tenants := tenantsFor(t, open)
	route := routeTo(t, server.URL, nil)
	route.TenantID = "ten_open"
	handler := publicHandler(t, map[string]routing.Route{"chat": route}, tenants)

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("open.hangar.dev", `{"model":"chat"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s, want 200 — auth: none takes no key at all", rec.Code, rec.Body.String())
	}
}

func TestTheInternalZoneNeverConsultsTenantsEvenWhenSomeAreConfigured(t *testing.T) {
	// The exact production shape today: a tenant exists, but Zone is left at
	// its default (ZoneInternal, since no listener sets it otherwise) and the
	// caller's Host is whatever Acme's edge happens to send — unrelated
	// to any tenant's host. This must behave exactly as it did with no
	// tenancy package at all.
	server, _ := upstreamServer(t)
	tenants := tenantsFor(t, globex())
	handler := New(Config{
		Router:  stubResolver{routes: map[string]routing.Route{"chat": routeTo(t, server.URL, nil)}},
		Tenants: tenants,
	})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("whatever-acme-sends", `{"model":"chat"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 — the internal zone must not start requiring tenants or keys", rec.Code)
	}
}
