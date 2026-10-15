// Package tenancy holds what the gateway needs to trust a tenant: which host
// names it, and which key hashes are allowed to speak for it (docs/decisions/0016).
//
// It refreshes on the same timer as the routing table and takes the same
// failure posture: the first load must succeed, and every one after that
// keeps serving the last good copy rather than answering an identified
// caller with a table that forgot who it was.
package tenancy

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

// Tenant is one tenant, as the gateway needs to trust it.
type Tenant struct {
	ID      string
	Slug    string
	Enabled bool
	// Ingress is "internal" or "public": which listener this tenant's traffic
	// is meant to arrive on.
	Ingress string
	// Auth is "none" or "api_key": whether a key is checked on this tenant's
	// requests.
	Auth string
	Host string
}

// Source is the tenant feed this package reads.
type Source interface {
	TenantsResolved(ctx context.Context, revision string) (plane.TenantTable, error)
}

// Table is the tenant picture at one revision.
//
// Two indexes over the same tenants, because the question the public
// listener asks is "does this key belong to the tenant this host names", not
// "does this key belong to some tenant". Looking the host and the key up
// separately and comparing the two tenants they resolve to is what keeps a
// key stolen from one tenant from authenticating as another; a single
// combined index could only ever answer the weaker question.
type Table struct {
	revision  string
	byHost    map[string]Tenant
	byKeyHash map[string]Tenant
	byID      map[string]Tenant
}

// TenantByHost resolves the tenant a request's Host header names.
func (t *Table) TenantByHost(host string) (Tenant, bool) {
	tenant, ok := t.byHost[host]
	return tenant, ok
}

// TenantByKeyHash resolves which tenant a presented key's hash belongs to.
func (t *Table) TenantByKeyHash(hash string) (Tenant, bool) {
	tenant, ok := t.byKeyHash[hash]
	return tenant, ok
}

// TenantByID resolves a tenant by its uid, which is how an endpoint names its
// owner in the routing table.
func (t *Table) TenantByID(id string) (Tenant, bool) {
	tenant, ok := t.byID[id]
	return tenant, ok
}

// Revision reports the table's current ETag.
func (t *Table) Revision() string {
	return t.revision
}

func empty() *Table {
	return &Table{byHost: map[string]Tenant{}, byKeyHash: map[string]Tenant{}, byID: map[string]Tenant{}}
}

// build turns the plane's feed into the two indexes Table serves from.
//
// A tenant with no host is skipped from byHost and reachable only by the
// internal listener — an internal tenant needs no host at all, since the
// listener itself is the claim.
//
// A disabled tenant the feed still lists is indexed like any other. Refusing it
// is the serving stages' job, so it gets a 403 that says so rather than reading
// as a host or a key nobody has heard of. A plane that leaves disabled tenants
// out of the feed instead gets the unknown-tenant answers: 404 unknown_host on
// the public listener, 403 tenant_disabled on the internal one.
func build(resolved plane.TenantTable) *Table {
	table := &Table{
		revision:  resolved.Revision,
		byHost:    make(map[string]Tenant, len(resolved.Tenants)),
		byKeyHash: make(map[string]Tenant),
		byID:      make(map[string]Tenant, len(resolved.Tenants)),
	}
	for _, gt := range resolved.Tenants {
		tenant := Tenant{
			ID:      gt.ID,
			Slug:    gt.Slug,
			Enabled: gt.Enabled,
			Ingress: gt.Ingress,
			Auth:    gt.Auth,
			Host:    gt.Host,
		}
		table.byID[gt.ID] = tenant
		if gt.Host != "" {
			table.byHost[gt.Host] = tenant
		}
		for _, hash := range gt.KeyHashes {
			table.byKeyHash[hash] = tenant
		}
	}
	return table
}

// DefaultMissRefreshInterval is the least time between two reloads caused by a
// lookup that missed (Store.RefreshOnMiss).
const DefaultMissRefreshInterval = 2 * time.Second

// missRefreshTimeout bounds one reload made on a request's behalf.
const missRefreshTimeout = 3 * time.Second

// Store holds the current table behind a lock, refreshed from Source.
type Store struct {
	source Source
	now    func() time.Time

	mu    sync.RWMutex
	table *Table

	// missMu serialises reloads made by RefreshOnMiss. Holding it across the
	// load is what coalesces them: a caller arriving during a reload waits for
	// it and then finds lastMiss recent.
	missMu      sync.Mutex
	missEvery   time.Duration
	lastMiss    time.Time
	lastMissErr error
}

// New builds an empty Store. Load must succeed once before Current serves
// anything a caller could be identified against.
func New(source Source) *Store {
	return &Store{source: source, now: time.Now, table: empty(), missEvery: DefaultMissRefreshInterval}
}

// RefreshOnMiss reloads the table because a lookup missed a tenant the caller
// has reason to believe exists: an endpoint in the routing table names an owner
// this table has not seen. The two tables resync on separate timers, so a
// tenant created moments ago can be in one and not yet the other.
//
// At most one reload per DefaultMissRefreshInterval, however many requests
// miss; callers inside that window get the last reload's result without
// touching the plane. A nil error means the table is at most that old, so a
// tenant still missing afterwards is one the plane does not list.
func (s *Store) RefreshOnMiss(ctx context.Context) error {
	s.missMu.Lock()
	defer s.missMu.Unlock()
	if !s.lastMiss.IsZero() && s.now().Sub(s.lastMiss) < s.missEvery {
		return s.lastMissErr
	}
	ctx, cancel := context.WithTimeout(ctx, missRefreshTimeout)
	defer cancel()
	err := s.Load(ctx)
	if err != nil {
		slog.Warn("gateway: tenant table reload after a missed lookup failed", "err", err)
	}
	s.lastMiss, s.lastMissErr = s.now(), err
	return err
}

// Current returns the table as of the last successful load.
func (s *Store) Current() *Table {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.table
}

// Load refreshes the table, or leaves it alone when the plane reports the
// current revision still holds.
func (s *Store) Load(ctx context.Context) error {
	s.mu.RLock()
	revision := s.table.revision
	s.mu.RUnlock()

	resolved, err := s.source.TenantsResolved(ctx, revision)
	if errors.Is(err, plane.ErrNotModified) {
		return nil
	}
	if err != nil {
		return err
	}

	next := build(resolved)
	s.mu.Lock()
	s.table = next
	s.mu.Unlock()
	return nil
}

// RunResync refreshes the table until the context is cancelled, keeping the
// last good copy on any failure — the same posture as routing.Router's own
// RunResync, and for the same reason: a tenant this hop cannot resolve right
// now must not become a tenant it has forgotten.
func (s *Store) RunResync(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := s.Load(ctx); err != nil {
				slog.Error("gateway: tenant table refresh failed, serving the table already loaded", "err", err)
			}
		}
	}
}
