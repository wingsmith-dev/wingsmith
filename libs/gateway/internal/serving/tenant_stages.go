package serving

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	"github.com/hangar-dev/hangar/libs/gateway/internal/tenancy"
)

// Zone says which listener accepted a connection: the one fact about a
// caller that nothing it sends can forge, and what the tenant and auth
// stages below rest their trust decision on (docs/decisions/0016).
//
// The zero value is no zone at all, and New refuses it. A listener states
// which zone it is; forgetting to must not quietly produce the zone that
// authenticates nobody.
type Zone int

const (
	// The zero Zone is no zone at all; New refuses it.
	_ Zone = iota
	// ZoneInternal is the unauthenticated serving port: a ClusterIP with no
	// ingress, reached only by the operator's own edge. A tenant on this zone
	// may declare auth: none, because the zone itself is the proof, which
	// holds only while a NetworkPolicy keeps everything else off the port
	// (libs/gateway/deploy/networkpolicy.yaml).
	ZoneInternal
	// ZonePublic is the *.hangar.example.com listener a tenant's own caller
	// reaches directly, served by HANGAR_GATEWAY_PUBLIC_LISTEN. A tenant is on
	// it once its row carries a host and its ingress is "public".
	ZonePublic
)

func (z Zone) valid() bool { return z == ZoneInternal || z == ZonePublic }

// String names the zone for logs and panics.
func (z Zone) String() string {
	switch z {
	case ZoneInternal:
		return "internal"
	case ZonePublic:
		return "public"
	default:
		return "unset"
	}
}

// Tenant ingress and endpoint visibility values, as the plane writes them.
const (
	ingressPublic    = "public"
	visibilityPublic = "public"
)

// Tenants is the tenant lookup the host and key stages need.
//
// One method, read on every request, instead of a snapshot handed to
// withRouting once at construction: `*tenancy.Store` refreshes on its own
// timer, and a stage that captured `Store.Current()` up front would keep
// answering from the table that was loaded when the handler was built,
// forever, no matter how many refreshes ran after it.
type Tenants interface {
	Current() *tenancy.Table
}

// stageZone records which listener this handler was built for.
//
// A closure over a fixed zone instead of something read per request: which
// socket accepted the connection is a fact about the listener, decided once
// at construction, and it is not derived from anything a caller
// sends: that is the whole reason it is what the trust decision rests on.
func stageZone(zone Zone) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		req.zone = zone
		return r, true
	}
}

// stageTenantFromHost resolves which tenant a request belongs to, from the
// `Host` header.
//
// Only the public zone resolves a tenant. The internal zone does
// not, and the consequence is worth stating plainly: an internal caller has no
// tenant, so it sees every enabled tenant's catalog and stageResolve's
// cross-tenant check never fires for it. The network boundary is what makes
// that safe (ADR 0016), not anything in this file, and `auth: none` on an
// internal tenant describes the zone instead of a decision about that tenant.
// The boundary is not optional: without the NetworkPolicy in
// libs/gateway/deploy/ anything in the cluster can reach that port.
//
// A tenant whose ingress is "internal" is not served here even when its row
// carries a host. It reads as a host nobody serves, the same answer an
// unknown host gets, because its traffic is meant to arrive through the edge.
func stageTenantFromHost(tenants Tenants) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if req.zone != ZonePublic {
			return r, true
		}
		tenant, ok := tenants.Current().TenantByHost(r.Host)
		if !ok || tenant.Ingress != ingressPublic {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusNotFound, "unknown_host", "This host does not serve any tenant.")
			return r, false
		}
		req.tenant = tenant
		return withTenantIdentity(r, tenant.ID, tenant.Slug), true
	}
}

// stageTenantEnabled refuses a public-zone request for a disabled tenant.
//
// After the key check, so a caller without the tenant's key learns nothing
// about whether it is enabled. The internal zone has no tenant at this point;
// its check is stageEndpointTenantEnabled, once the model names one.
func stageTenantEnabled(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	if req.zone != ZonePublic || req.tenant.Enabled {
		return r, true
	}
	refuseDisabledTenant(w, r)
	return r, false
}

// stageEndpointTenantEnabled refuses a request whose endpoint belongs to a
// disabled tenant, on either zone.
//
// The plane is expected to stop listing a disabled tenant's endpoints, but the
// routing table and the tenant table refresh separately, and a routing table
// that has not caught up must not keep serving a tenant somebody switched off.
// So the tenant table is asked as well.
//
// A tenant the table does not know is either new (its endpoint reached the
// routing table first) or gone (the plane may drop a disabled tenant from the
// feed outright). The two look the same until the tenant table is reloaded, so
// a miss reloads it, coalesced and rate-limited (tenancy.Store.RefreshOnMiss),
// and asks again. Still missing after a successful reload means the plane does
// not list it, and it is refused like a disabled tenant. A reload that failed
// answers 503: this hop cannot vouch for the owner right now, and calling a
// tenant created a second ago "disabled" would be wrong.
//
// An endpoint with no owning tenant, or a handler built with no tenant table,
// is left alone.
func stageEndpointTenantEnabled(tenants Tenants) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if tenants == nil || req.route.TenantID == "" {
			return r, true
		}
		if req.zone == ZonePublic {
			// Already checked: stageResolve has made the route's tenant the
			// host's, and stageTenantEnabled has refused it if disabled.
			return r, true
		}
		owner, ok := tenants.Current().TenantByID(req.route.TenantID)
		if !ok {
			if refresher, can := tenants.(missRefresher); can {
				if err := refresher.RefreshOnMiss(r.Context()); err != nil {
					// Shed, not internal_error: the plane is what is
					// missing, and the caller can retry.
					w.Header().Set("Retry-After", "2")
					MarkOutcome(r, metrics.OutcomeShed)
					writeError(w, http.StatusServiceUnavailable, "tenant_unverified",
						"Unable to verify this model's tenant right now. Retry shortly.")
					return r, false
				}
				owner, ok = tenants.Current().TenantByID(req.route.TenantID)
			}
		}
		if ok && owner.Enabled {
			return r, true
		}
		refuseDisabledTenant(w, r)
		return r, false
	}
}

// missRefresher is the optional half of Tenants: a table that can be reloaded
// when a lookup misses. *tenancy.Store implements it.
type missRefresher interface {
	RefreshOnMiss(ctx context.Context) error
}

// tenantServing reports whether a route's tenant is one this hop may serve.
// Used by the catalog, which must not list what a request would be refused.
func tenantServing(tenants Tenants, route routing.Route) bool {
	if tenants == nil || route.TenantID == "" {
		return true
	}
	owner, ok := tenants.Current().TenantByID(route.TenantID)
	return ok && owner.Enabled
}

func refuseDisabledTenant(w http.ResponseWriter, r *http.Request) {
	MarkOutcome(r, metrics.OutcomeAuthRejected)
	writeError(w, http.StatusForbidden, "tenant_disabled", "This tenant is disabled.")
}

// stageKeyCheck enforces `auth: api_key` on the public zone.
//
// The internal zone proves nothing beyond having reached the port, which
// `auth: none` there already reflects; this stage is a no-op for it. A key
// is checked against the tenant `stageTenantFromHost` resolved from the
// `Host` header, not against any tenant a valid key happens to name: a key
// stolen from one tenant must not authenticate as another just because it
// hashes to a row in the table.
//
// A public tenant declaring anything but `api_key` is refused unless the
// gateway was started with allowNoAuth: a tenant row alone must not be able to
// open an unauthenticated public endpoint.
func stageKeyCheck(tenants Tenants, allowNoAuth bool) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if req.zone != ZonePublic {
			return r, true
		}
		if req.tenant.Auth != "api_key" {
			if allowNoAuth {
				return r, true
			}
			MarkOutcome(r, metrics.OutcomeAuthRejected)
			writeError(w, http.StatusUnauthorized, "unauthorized",
				"This tenant does not accept unauthenticated requests on this listener.")
			return r, false
		}
		presented, ok := bearerToken(r)
		if !ok {
			MarkOutcome(r, metrics.OutcomeAuthRejected)
			writeError(w, http.StatusUnauthorized, "unauthorized", "This tenant requires a key.")
			return r, false
		}
		// Recorded before the lookup, not after it, so a rejection is
		// logged against the key that was tried. A refusal nobody can attribute
		// is the one an on-call cannot act on: "someone is sending a stale key"
		// and "this key is stale" are the same line with and without this.
		hash := keyHash(presented)
		r = withKeyIdentity(r, hash)

		owner, ok := tenants.Current().TenantByKeyHash(hash)
		if !ok || owner.ID != req.tenant.ID {
			MarkOutcome(r, metrics.OutcomeAuthRejected)
			writeError(w, http.StatusUnauthorized, "unauthorized", "This key does not belong to this tenant.")
			return r, false
		}
		return r, true
	}
}

// bearerToken reads the caller's presented key off the Authorization header.
func bearerToken(r *http.Request) (string, bool) {
	return strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
}

// keyHash is the sha256 hex digest a presented key is looked up by: the
// same digest the plane stores, so a key is never compared to anything in
// its plaintext form on this hop or that one. A digest looked up by map key,
// not compared byte-by-byte against a small fixed set, which is why
// this needs no constant-time comparison the way the retired shared-token
// guard's did.
func keyHash(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}
