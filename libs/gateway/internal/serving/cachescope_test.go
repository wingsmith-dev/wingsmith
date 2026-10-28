package serving

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/routing"
)

const sharedPrompt = `{"model":"chat","messages":[{"role":"system","content":"a long shared system prompt"},` +
	`{"role":"user","content":"hello"}]}`

// arrivedSalt is the cache_salt the cluster received, or "" when none was written.
func arrivedSalt(t *testing.T, body []byte) string {
	t.Helper()
	var fields struct {
		NvExt struct {
			CacheSalt string `json:"cache_salt"`
		} `json:"nvext"`
	}
	if err := json.Unmarshal(body, &fields); err != nil {
		t.Fatalf("arrived body is not JSON: %v: %s", err, body)
	}
	return fields.NvExt.CacheSalt
}

func scopedSalt(t *testing.T, body string, scope cacheScope) string {
	t.Helper()
	out, written, err := withCacheSalt([]byte(body), scope)
	if err != nil {
		t.Fatalf("withCacheSalt: %v", err)
	}
	if !written {
		t.Fatalf("no salt written for scope %+v", scope)
	}
	return arrivedSalt(t, out)
}

// TestTwoTenantsSendingOnePromptNeverShareACacheIdentity is the side channel this
// closes: the same text from two tenants on one deployment must hash apart, or one
// tenant can learn from time to first token what the other recently sent.
func TestTwoTenantsSendingOnePromptNeverShareACacheIdentity(t *testing.T) {
	acme := scopedSalt(t, sharedPrompt, cacheScope{Tenant: "ten_acme"})
	globex := scopedSalt(t, sharedPrompt, cacheScope{Tenant: "ten_globex"})
	if acme == globex {
		t.Errorf("two tenants share the salt %q", acme)
	}
}

// TestOneTenantKeepsItsOwnReuse is the other half: the salt is per tenant, not per
// request, so a tenant's repeated prefix still hits its own cache.
func TestOneTenantKeepsItsOwnReuse(t *testing.T) {
	first := scopedSalt(t, sharedPrompt, cacheScope{Tenant: "ten_acme"})
	second := scopedSalt(t, `{"model":"chat","messages":[{"role":"system","content":"a long shared system prompt"},`+
		`{"role":"user","content":"a different question"}]}`, cacheScope{Tenant: "ten_acme"})
	if first != second {
		t.Errorf("one tenant's text requests hashed two ways: %q and %q", first, second)
	}
}

// TestAnInternalCallersScopeNarrowsTheTenant is a platform serving many customers
// through one tenant: each customer it names gets its own cache.
func TestAnInternalCallersScopeNarrowsTheTenant(t *testing.T) {
	a := scopedSalt(t, sharedPrompt, cacheScope{Tenant: "ten_platform", Caller: "org_a"})
	b := scopedSalt(t, sharedPrompt, cacheScope{Tenant: "ten_platform", Caller: "org_b"})
	whole := scopedSalt(t, sharedPrompt, cacheScope{Tenant: "ten_platform"})
	if a == b || a == whole || b == whole {
		t.Errorf("caller scopes did not separate: %q %q %q", a, b, whole)
	}
}

// TestACallerCannotNameAnotherTenantsSalt pins the fold: a caller that knows, or
// guesses, the salt another tenant's requests carry still lands in its own scope.
func TestACallerCannotNameAnotherTenantsSalt(t *testing.T) {
	victim := scopedSalt(t, sharedPrompt, cacheScope{Tenant: "ten_victim"})
	forged := strings.TrimSuffix(sharedPrompt, "}") + `,"nvext":{"cache_salt":` + mustJSON(t, victim) + `}}`
	if got := scopedSalt(t, forged, cacheScope{Tenant: "ten_attacker"}); got == victim {
		t.Error("a caller's own cache_salt reproduced another tenant's salt")
	}
}

// TestTheMediaIdentityStillHoldsInsideATenant keeps the media identity fix: two images
// from one tenant are still two identities.
func TestTheMediaIdentityStillHoldsInsideATenant(t *testing.T) {
	scope := cacheScope{Tenant: "ten_acme"}
	if first, second := scopedSalt(t, imageBody(t, "first"), scope), scopedSalt(t, imageBody(t, "second"), scope); first == second {
		t.Errorf("two images inside one tenant share the salt %q", first)
	}
	if mine, theirs := scopedSalt(t, imageBody(t, "one"), scope), scopedSalt(t, imageBody(t, "one"), cacheScope{Tenant: "ten_globex"}); mine == theirs {
		t.Errorf("one image from two tenants shares the salt %q", mine)
	}
}

func TestOnlyTextGenerationPathsAreScoped(t *testing.T) {
	text := routing.Route{TenantID: "ten_acme"}
	image := routing.Route{TenantID: "ten_acme", Upstreams: []routing.Upstream{{OutputModality: modalityImage}}}
	for _, tc := range []struct {
		path   string
		route  routing.Route
		scoped bool
	}{
		{"/v1/chat/completions", text, true},
		{"/v1/completions", text, true},
		{"/v1/responses", text, true},
		{"/v1/messages", text, true},
		{"/v1/embeddings", text, false},
		{"/v1/images/generations", image, false},
		{"/v1/chat/completions", image, false},
	} {
		r := httptest.NewRequest(http.MethodPost, tc.path, nil)
		scope := requestCacheScope(r, &requestState{route: tc.route})
		if got := !scope.empty(); got != tc.scoped {
			t.Errorf("%s on a %q route: scoped = %v, want %v", tc.path, routeModality(tc.route), got, tc.scoped)
		}
	}
}

// TestThePublicZoneScopesByTheHostsTenantAndIgnoresTheScopeHeader runs the whole
// pipeline: the salt reaches the cluster, it is the host's tenant's, and a public
// caller's X-Wingsmith-Cache-Scope neither changes it nor reaches the cluster.
func TestThePublicZoneScopesByTheHostsTenantAndIgnoresTheScopeHeader(t *testing.T) {
	server, seen := steeringUpstream(t)
	handler := publicHandler(t, map[string]routing.Route{"chat": globexRoute(t, server.URL)}, tenantsFor(t, globex()))

	send := func(headers ...string) string {
		rec := httptest.NewRecorder()
		headers = append(headers, "Authorization", "Bearer secret-key")
		handler.ServeHTTP(rec, requestTo("globex.wingsmith.dev", sharedPrompt, headers...))
		if rec.Code != http.StatusOK {
			t.Fatalf("status = %d, body = %s", rec.Code, rec.Body)
		}
		if got := seen.header.Get(cacheScopeHeader); got != "" {
			t.Errorf("%s reached the cluster as %q", cacheScopeHeader, got)
		}
		return arrivedSalt(t, seen.body)
	}

	plain := send()
	if want := scopedSalt(t, sharedPrompt, cacheScope{Tenant: "ten_globex"}); plain != want {
		t.Errorf("salt = %q, want the tenant's %q", plain, want)
	}
	if forged := send(cacheScopeHeader, "org_someone_else"); forged != plain {
		t.Errorf("a public caller's scope header changed the salt to %q", forged)
	}
}

// TestTheInternalZoneScopesByTheOwnerNarrowedByTheHeader is the same pipeline on the
// internal port, where the caller is trusted to name its customer.
func TestTheInternalZoneScopesByTheOwnerNarrowedByTheHeader(t *testing.T) {
	server, seen := steeringUpstream(t)
	route := routeTo(t, server.URL, nil)
	route.TenantID = "ten_platform"

	sendThrough(t, route, sharedPrompt, http.Header{cacheScopeHeader: {"org_a"}})
	if got := seen.header.Get(cacheScopeHeader); got != "" {
		t.Errorf("%s reached the cluster as %q", cacheScopeHeader, got)
	}
	want := scopedSalt(t, sharedPrompt, cacheScope{Tenant: "ten_platform", Caller: "org_a"})
	if got := arrivedSalt(t, seen.body); got != want {
		t.Errorf("salt = %q, want %q", got, want)
	}
}

// TestAnEndpointWithNoOwnerIsForwardedAsTheBytesThatArrived keeps the old text path
// where there is no tenant to scope by.
func TestAnEndpointWithNoOwnerIsForwardedAsTheBytesThatArrived(t *testing.T) {
	server, seen := steeringUpstream(t)
	sendThrough(t, routeTo(t, server.URL, nil), sharedPrompt, nil)
	if string(seen.body) != sharedPrompt {
		t.Errorf("body was rewritten:\n got %s\nwant %s", seen.body, sharedPrompt)
	}
}
