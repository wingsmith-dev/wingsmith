package serving

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/tenancy"
)

// Zone says which listener accepted a connection — the one fact about a
// caller that nothing it sends can forge, and what the tenant and auth
// stages below rest their trust decision on (docs/plans/17).
type Zone int

const (
	// ZoneInternal is the existing serving port: ClusterIP, no ingress,
	// reached only by systems this fleet runs. A tenant on this zone may
	// declare auth: none, because the zone itself is the proof.
	ZoneInternal Zone = iota
	// ZonePublic is the *.hangar.example.com listener a tenant's own caller
	// reaches directly, served by HANGAR_GATEWAY_PUBLIC_LISTEN. A tenant is on
	// it once its row carries a host; one whose host is empty is not in the
	// index at all and stays reachable only on the internal zone.
	ZonePublic
)

// Tenants is the tenant lookup the host and key stages need.
//
// One method, read on every request, rather than a snapshot handed to
// withRouting once at construction: `*tenancy.Store` refreshes on its own
// timer, and a stage that captured `Store.Current()` up front would keep
// answering from the table that was loaded when the handler was built,
// forever, no matter how many refreshes ran after it.
type Tenants interface {
	Current() *tenancy.Table
}

// stageZone records which listener this handler was built for.
//
// A closure over a fixed zone rather than something read per request: which
// socket accepted the connection is a fact about the listener, decided once
// at construction, and it is deliberately not derived from anything a caller
// sends — that is the whole reason it is what the trust decision rests on.
func stageZone(zone Zone) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		req.zone = zone
		return r, true
	}
}

// stageTenantFromHost resolves which tenant a request belongs to, from the
// `Host` header.
//
// Only the public zone resolves a tenant. The internal zone deliberately does
// not, and the consequence is worth stating plainly: an internal caller has no
// tenant, so it sees the whole catalog and stageResolve's cross-tenant check
// never fires for it — every tenant's models are reachable on that port,
// including ones that are otherwise public and key-gated. The boundary is what
// makes that safe (ADR 0016), not anything in this file, and `auth: none` on an
// internal tenant describes the zone rather than a decision about that tenant.
func stageTenantFromHost(tenants Tenants) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if req.zone != ZonePublic {
			return r, true
		}
		tenant, ok := tenants.Current().TenantByHost(r.Host)
		if !ok {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusNotFound, "unknown_host", "This host does not serve any tenant.")
			return r, false
		}
		req.tenant = tenant
		return withTenantIdentity(r, tenant.ID, tenant.Slug), true
	}
}

// stageKeyCheck enforces `auth: api_key` on the public zone.
//
// The internal zone proves nothing beyond having reached the port, which
// `auth: none` there already reflects — this stage is a no-op for it. A key
// is checked against the tenant `stageTenantFromHost` resolved from the
// `Host` header, not against any tenant a valid key happens to name: a key
// stolen from one tenant must not authenticate as another just because it
// hashes to a row in the table.
func stageKeyCheck(tenants Tenants) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if req.zone != ZonePublic || req.tenant.Auth != "api_key" {
			return r, true
		}
		presented, ok := bearerToken(r)
		if !ok {
			MarkOutcome(r, metrics.OutcomeAuthRejected)
			writeError(w, http.StatusUnauthorized, "unauthorized", "This tenant requires a key.")
			return r, false
		}
		// Recorded before the lookup rather than after it, so a rejection is
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

// keyHash is the sha256 hex digest a presented key is looked up by — the
// same digest the plane stores, so a key is never compared to anything in
// its plaintext form on this hop or that one. A digest looked up by map key
// rather than compared byte-by-byte against a small fixed set, which is why
// this needs no constant-time comparison the way the retired shared-token
// guard's did.
func keyHash(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}
