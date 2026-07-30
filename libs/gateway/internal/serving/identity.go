package serving

import (
	"context"
	"net/http"
	"sync"
)

// keyPrefixLen is how much of a key's sha256 digest the access log carries.
//
// Enough to tell two of a tenant's keys apart in a query, short enough that the
// line is not a second copy of the credential store: the full digest is what the
// plane stores and what stageKeyCheck looks a caller up by, so logging all of it
// would put the lookup value itself in a stream with a different retention and a
// different audience.
const keyPrefixLen = 12

// callerIdentity is who the access log says a request was, and it exists
// separately from `observation` on purpose.
//
// Identity may not go on `observation` (see observe.go): everything on that
// struct becomes a metric label, and a key or a tenant on a label is unbounded
// cardinality. A log line is not a label — it is written once, rotated, and
// queried by grep rather than by PromQL — so the same fact is safe here and
// unsafe there. Keeping them in two types is what stops a later change from
// quietly promoting one into the other.
//
// It is also not a ledger. The counts on the line beside it come from the
// upstream's own usage object and are observability, the same posture the
// metrics package states: nothing here is metered or billed.
//
// A pointer in the context rather than a value, for the reason `observation`
// is one: the stages that learn a tenant and a key run below the wrapper that
// writes the line, and a value re-wrapped onto the request there would never
// be visible to the `r` the wrapper closed over.
type callerIdentity struct {
	mu sync.Mutex
	// tenantID and tenantSlug are the plane's own identifiers, empty on the
	// internal zone, which resolves no tenant at all.
	tenantID   string
	tenantSlug string
	// keyPrefix is the first keyPrefixLen hex characters of the presented
	// key's sha256 digest, empty when no key was checked.
	keyPrefix string
}

type callerIdentityKey struct{}

// newIdentity attaches a fresh identity to a request, once, at the top.
func newIdentity(r *http.Request) (*http.Request, *callerIdentity) {
	ident := &callerIdentity{}
	return r.WithContext(context.WithValue(r.Context(), callerIdentityKey{}, ident)), ident
}

// identityFrom returns the identity carried by a request, if any.
func identityFrom(ctx context.Context) *callerIdentity {
	ident, _ := ctx.Value(callerIdentityKey{}).(*callerIdentity)
	return ident
}

// withTenantIdentity records which tenant a request resolved to.
func withTenantIdentity(r *http.Request, id, slug string) *http.Request {
	if ident := identityFrom(r.Context()); ident != nil {
		ident.mu.Lock()
		ident.tenantID, ident.tenantSlug = id, slug
		ident.mu.Unlock()
	}
	return r
}

// withKeyIdentity records which key spoke for it.
//
// It takes the digest rather than the key, so a plaintext credential never
// reaches this package's call stack at all.
func withKeyIdentity(r *http.Request, hash string) *http.Request {
	if ident := identityFrom(r.Context()); ident != nil {
		if len(hash) > keyPrefixLen {
			hash = hash[:keyPrefixLen]
		}
		ident.mu.Lock()
		ident.keyPrefix = hash
		ident.mu.Unlock()
	}
	return r
}

// fields freezes the identity into the attributes the access log carries.
//
// Absent rather than empty: a request on the internal zone has no tenant, and a
// line that said tenant="" would read as a tenant whose name is empty and would
// aggregate with every other one in a query that groups by it.
func (i *callerIdentity) fields() []any {
	if i == nil {
		return nil
	}
	i.mu.Lock()
	defer i.mu.Unlock()

	out := make([]any, 0, 6)
	if i.tenantID != "" {
		out = append(out, "tenant_id", i.tenantID, "tenant", i.tenantSlug)
	}
	if i.keyPrefix != "" {
		out = append(out, "key", i.keyPrefix)
	}
	return out
}
