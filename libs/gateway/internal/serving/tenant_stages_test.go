package serving

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
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

// tenantsFor builds a loaded *tenancy.Store the same way production does,
// through Load against a Source, instead of reaching into the package for
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
	route.Visibility = "public"
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
		t.Fatalf("status = %d, want 401: a key minted for a different tenant must not pass", rec.Code)
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
	acmesChat.Visibility = "public"
	handler := publicHandler(t, map[string]routing.Route{"chat": acmesChat}, tenants)

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("globex.hangar.dev", `{"model":"chat"}`, "Authorization", "Bearer secret-key"))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404: a name resolving to another tenant's endpoint must read as absent", rec.Code)
	}
}

func TestTheCatalogOnThePublicZoneListsOnlyThisTenantsModels(t *testing.T) {
	server, _ := upstreamServer(t)
	tenants := tenantsFor(t, globex())
	acmesChat := routeTo(t, server.URL, nil)
	acmesChat.TenantID = "ten_acme"
	acmesChat.Visibility = "public"
	handler := publicHandler(t, map[string]routing.Route{
		"chat":   globexRoute(t, server.URL),
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
	route.Visibility = "public"
	handler := New(Config{
		Router:                     stubResolver{routes: map[string]routing.Route{"chat": route}},
		Tenants:                    tenants,
		Zone:                       ZonePublic,
		AllowUnauthenticatedPublic: true,
	})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("open.hangar.dev", `{"model":"chat"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s, want 200; auth: none takes no key at all", rec.Code, rec.Body.String())
	}

	// Without the gateway's own opt-in, the tenant row alone does not open an
	// unauthenticated public endpoint.
	refusing := publicHandler(t, map[string]routing.Route{"chat": route}, tenants)
	rec = httptest.NewRecorder()
	refusing.ServeHTTP(rec, requestTo("open.hangar.dev", `{"model":"chat"}`))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401 without AllowUnauthenticatedPublic", rec.Code)
	}
}

func TestTheInternalZoneNeverConsultsTenantsEvenWhenSomeAreConfigured(t *testing.T) {
	// A tenant exists and the caller's Host is whatever the operator's edge
	// happens to send, unrelated to any tenant's host. The internal zone must
	// not start requiring tenants or keys.
	server, _ := upstreamServer(t)
	tenants := tenantsFor(t, globex())
	handler := New(Config{
		Zone:    ZoneInternal,
		Router:  stubResolver{routes: map[string]routing.Route{"chat": routeTo(t, server.URL, nil)}},
		Tenants: tenants,
	})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("whatever-acme-sends", `{"model":"chat"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: the internal zone must not start requiring tenants or keys", rec.Code)
	}
}

func disabled(tenant plane.GatewayTenant) plane.GatewayTenant {
	tenant.Enabled = false
	return tenant
}

func TestADisabledTenantIsRefusedOnThePublicZone(t *testing.T) {
	server, _ := upstreamServer(t)
	handler := publicHandler(t, map[string]routing.Route{"chat": globexRoute(t, server.URL)}, tenantsFor(t, disabled(globex())))

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("globex.hangar.dev", `{"model":"chat"}`, "Authorization", "Bearer secret-key"))
	if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "tenant_disabled") {
		t.Fatalf("status = %d, body = %s, want 403 tenant_disabled", rec.Code, rec.Body)
	}

	// Without the key it is still the key that is refused: whether a tenant is
	// enabled is not something an unauthenticated caller gets to learn.
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("globex.hangar.dev", `{"model":"chat"}`))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401 before the enabled check", rec.Code)
	}
}

// TestADisabledTenantsEndpointIsRefusedOnTheInternalZone is the case that
// matters most: the internal zone carries the edge's traffic for every tenant,
// and a routing table that still lists a disabled tenant's endpoint must not
// keep serving it.
func TestADisabledTenantsEndpointIsRefusedOnTheInternalZone(t *testing.T) {
	server, _ := upstreamServer(t)
	route := globexRoute(t, server.URL)
	route.Visibility = "private"

	for _, test := range []struct {
		name    string
		tenants *tenancy.Store
		status  int
	}{
		{name: "enabled", tenants: tenantsFor(t, globex()), status: http.StatusOK},
		{name: "disabled", tenants: tenantsFor(t, disabled(globex())), status: http.StatusForbidden},
		{name: "absent from the tenant feed", tenants: tenantsFor(t), status: http.StatusForbidden},
	} {
		t.Run(test.name, func(t *testing.T) {
			handler := New(Config{
				Router:  stubResolver{routes: map[string]routing.Route{"chat": route}},
				Tenants: test.tenants,
				Zone:    ZoneInternal,
			})
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, requestTo("edge", `{"model":"chat"}`))
			if rec.Code != test.status {
				t.Fatalf("status = %d, body = %s, want %d", rec.Code, rec.Body, test.status)
			}

			catalog := httptest.NewRecorder()
			handler.ServeHTTP(catalog, httptest.NewRequest(http.MethodGet, "/v1/models", nil))
			listed := strings.Contains(catalog.Body.String(), `"chat"`)
			if listed != (test.status == http.StatusOK) {
				t.Fatalf("catalog = %s, listed should be %v", catalog.Body, !listed)
			}
		})
	}
}

// sequencedTenantSource answers each read with the next table, or the next
// error, and repeats the last table once it runs out.
type sequencedTenantSource struct {
	mu     sync.Mutex
	tables []plane.TenantTable
	errs   []error
	reads  int
}

func (s *sequencedTenantSource) TenantsResolved(_ context.Context, _ string) (plane.TenantTable, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	i := s.reads
	s.reads++
	if i < len(s.errs) && s.errs[i] != nil {
		return plane.TenantTable{}, s.errs[i]
	}
	if i >= len(s.tables) {
		i = len(s.tables) - 1
	}
	return s.tables[i], nil
}

// The routing table and the tenant table resync separately, so an endpoint of a
// tenant created a moment ago can arrive before its tenant does. That must not
// read as a disabled tenant: the miss reloads the tenant table and serves.
func TestANewTenantsEndpointIsServedBeforeTheTenantTableResyncs(t *testing.T) {
	server, _ := upstreamServer(t)
	route := globexRoute(t, server.URL)
	route.Visibility = "private"
	source := &sequencedTenantSource{tables: []plane.TenantTable{
		{Revision: "1"},
		{Revision: "2", Tenants: []plane.GatewayTenant{globex()}},
	}}
	store := tenancy.New(source)
	if err := store.Load(context.Background()); err != nil {
		t.Fatal(err)
	}
	handler := New(Config{
		Router:  stubResolver{routes: map[string]routing.Route{"chat": route}},
		Tenants: store,
		Zone:    ZoneInternal,
	})
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("edge", `{"model":"chat"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s, want 200 after the reload", rec.Code, rec.Body)
	}
}

// When the reload itself fails, the owner can be neither confirmed nor ruled
// out, and the answer is a retryable 503 instead of "disabled".
func TestAnUnknownOwnerIsUnavailableWhileThePlaneCannotBeRead(t *testing.T) {
	server, _ := upstreamServer(t)
	route := globexRoute(t, server.URL)
	route.Visibility = "private"
	source := &sequencedTenantSource{
		tables: []plane.TenantTable{{Revision: "1"}},
		errs:   []error{nil, errors.New("plane down")},
	}
	store := tenancy.New(source)
	if err := store.Load(context.Background()); err != nil {
		t.Fatal(err)
	}
	handler := New(Config{
		Router:  stubResolver{routes: map[string]routing.Route{"chat": route}},
		Tenants: store,
		Zone:    ZoneInternal,
	})
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("edge", `{"model":"chat"}`))
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "tenant_unverified") {
		t.Fatalf("status = %d, body = %s, want 503 tenant_unverified", rec.Code, rec.Body)
	}
	if rec.Header().Get("Retry-After") == "" {
		t.Fatal("no Retry-After on a retryable refusal")
	}
}

func TestAnEndpointWithNoOwnerIsServedOnTheInternalZone(t *testing.T) {
	server, _ := upstreamServer(t)
	handler := New(Config{
		Router:  stubResolver{routes: map[string]routing.Route{"chat": routeTo(t, server.URL, nil)}},
		Tenants: tenantsFor(t),
		Zone:    ZoneInternal,
	})
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("edge", `{"model":"chat"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
}

// TestAPrivateEndpointIsNotServedOnThePublicZone pins what `visibility`
// means: private, the plane's default, is internal-only, and on the public
// listener it reads as a model that does not exist instead of a refusal.
func TestAPrivateEndpointIsNotServedOnThePublicZone(t *testing.T) {
	server, _ := upstreamServer(t)
	route := globexRoute(t, server.URL)
	route.Visibility = "private"
	handler := publicHandler(t, map[string]routing.Route{"chat": route}, tenantsFor(t, globex()))

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("globex.hangar.dev", `{"model":"chat"}`, "Authorization", "Bearer secret-key"))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Host = "globex.hangar.dev"
	req.Header.Set("Authorization", "Bearer secret-key")
	catalog := httptest.NewRecorder()
	handler.ServeHTTP(catalog, req)
	if strings.Contains(catalog.Body.String(), `"chat"`) {
		t.Fatalf("catalog lists a private endpoint: %s", catalog.Body)
	}
}

func TestATenantWithInternalIngressIsNotServedOnThePublicZone(t *testing.T) {
	server, _ := upstreamServer(t)
	tenant := globex()
	tenant.Ingress = "internal"
	handler := publicHandler(t, map[string]routing.Route{"chat": globexRoute(t, server.URL)}, tenantsFor(t, tenant))

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, requestTo("globex.hangar.dev", `{"model":"chat"}`, "Authorization", "Bearer secret-key"))
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "unknown_host") {
		t.Fatalf("status = %d, body = %s, want 404 unknown_host", rec.Code, rec.Body)
	}
}

func TestAHandlerWithNoZoneIsRefused(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("New accepted a Config with no zone")
		}
	}()
	New(Config{Router: stubResolver{}})
}
