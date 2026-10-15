package tenancy

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

type stubSource struct {
	tables []plane.TenantTable
	errs   []error
	calls  []string
}

func (s *stubSource) TenantsResolved(_ context.Context, revision string) (plane.TenantTable, error) {
	s.calls = append(s.calls, revision)
	i := len(s.calls) - 1
	if i < len(s.errs) && s.errs[i] != nil {
		return plane.TenantTable{}, s.errs[i]
	}
	if i < len(s.tables) {
		return s.tables[i], nil
	}
	return plane.TenantTable{}, plane.ErrNotModified
}

func globex() plane.GatewayTenant {
	return plane.GatewayTenant{
		ID:        "ten_globex",
		Slug:      "globex",
		Enabled:   true,
		Ingress:   "public",
		Auth:      "api_key",
		Host:      "globex.hangar.dev",
		KeyHashes: []string{"livehash"},
	}
}

func acme() plane.GatewayTenant {
	return plane.GatewayTenant{
		ID:      "ten_acme",
		Slug:    "acme",
		Enabled: true,
		Ingress: "internal",
		Auth:    "none",
	}
}

func TestANewStoreServesAnEmptyTableBeforeAnyLoad(t *testing.T) {
	store := New(&stubSource{})
	table := store.Current()
	if _, ok := table.TenantByHost("globex.hangar.dev"); ok {
		t.Fatal("an unloaded store should resolve nothing")
	}
	if table.Revision() != "" {
		t.Fatalf("revision = %q, want empty", table.Revision())
	}
}

func TestALoadedTableResolvesATenantByItsHostAndByItsKeyHash(t *testing.T) {
	source := &stubSource{tables: []plane.TenantTable{{Revision: "1-1", Tenants: []plane.GatewayTenant{globex()}}}}
	store := New(source)
	if err := store.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}

	table := store.Current()
	byHost, ok := table.TenantByHost("globex.hangar.dev")
	if !ok || byHost.ID != "ten_globex" {
		t.Fatalf("TenantByHost = %+v, %v", byHost, ok)
	}
	byKey, ok := table.TenantByKeyHash("livehash")
	if !ok || byKey.ID != "ten_globex" {
		t.Fatalf("TenantByKeyHash = %+v, %v", byKey, ok)
	}
	if table.Revision() != "1-1" {
		t.Fatalf("revision = %q, want 1-1", table.Revision())
	}
}

func TestATenantWithNoHostIsNotInTheHostIndex(t *testing.T) {
	source := &stubSource{tables: []plane.TenantTable{{Revision: "1-1", Tenants: []plane.GatewayTenant{acme()}}}}
	store := New(source)
	if err := store.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	if _, ok := store.Current().TenantByHost(""); ok {
		t.Fatal("an internal tenant with no host must not resolve against an empty Host header")
	}
}

func TestNotModifiedLeavesTheCurrentTableInPlace(t *testing.T) {
	source := &stubSource{tables: []plane.TenantTable{{Revision: "1-1", Tenants: []plane.GatewayTenant{globex()}}}}
	store := New(source)
	if err := store.Load(context.Background()); err != nil {
		t.Fatalf("first load: %v", err)
	}
	before := store.Current()

	if err := store.Load(context.Background()); err != nil {
		t.Fatalf("second load: %v", err)
	}
	if store.Current() != before {
		t.Fatal("a 304 must not replace the table that is already loaded")
	}
}

func TestAFailedRefreshKeepsServingTheLastGoodTable(t *testing.T) {
	source := &stubSource{
		tables: []plane.TenantTable{{Revision: "1-1", Tenants: []plane.GatewayTenant{globex()}}},
		errs:   []error{nil, errors.New("plane unreachable")},
	}
	store := New(source)
	if err := store.Load(context.Background()); err != nil {
		t.Fatalf("first load: %v", err)
	}
	if err := store.Load(context.Background()); err == nil {
		t.Fatal("expected the second load to report the plane's error")
	}

	if _, ok := store.Current().TenantByHost("globex.hangar.dev"); !ok {
		t.Fatal("a failed refresh must not drop the table already loaded")
	}
}

func TestRunResyncStopsWhenItsContextIsCancelled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	store := New(&stubSource{})
	done := make(chan struct{})
	go func() {
		store.RunResync(ctx, time.Millisecond)
		close(done)
	}()
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("RunResync did not stop after its context was cancelled")
	}
}

func TestEveryTenantIsIndexedByIDWithItsEnabledFlag(t *testing.T) {
	off := acme()
	off.Enabled = false
	source := &stubSource{tables: []plane.TenantTable{{Revision: "1-2", Tenants: []plane.GatewayTenant{globex(), off}}}}
	store := New(source)
	if err := store.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	table := store.Current()
	if tenant, ok := table.TenantByID("ten_globex"); !ok || !tenant.Enabled {
		t.Fatalf("TenantByID(globex) = %+v, %v", tenant, ok)
	}
	// Indexed, not dropped: the serving stages refuse it with a 403 that says
	// it is disabled, which they can only do if they can find it.
	if tenant, ok := table.TenantByID("ten_acme"); !ok || tenant.Enabled {
		t.Fatalf("TenantByID(acme) = %+v, %v, want it found and disabled", tenant, ok)
	}
}

func TestRefreshOnMissPicksUpATenantCreatedSinceTheLastLoad(t *testing.T) {
	source := &stubSource{tables: []plane.TenantTable{
		{Revision: "1", Tenants: []plane.GatewayTenant{globex()}},
		{Revision: "2", Tenants: []plane.GatewayTenant{globex(), acme()}},
	}}
	store := New(source)
	if err := store.Load(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, ok := store.Current().TenantByID("ten_acme"); ok {
		t.Fatal("acme is not in the first table")
	}
	if err := store.RefreshOnMiss(context.Background()); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if _, ok := store.Current().TenantByID("ten_acme"); !ok {
		t.Fatal("a reload on a miss should have picked up acme")
	}
}

func TestRefreshOnMissReloadsAtMostOncePerInterval(t *testing.T) {
	source := &stubSource{tables: []plane.TenantTable{{Revision: "1", Tenants: []plane.GatewayTenant{globex()}}}}
	store := New(source)
	clock := time.Unix(1000, 0)
	store.now = func() time.Time { return clock }

	for range 5 {
		_ = store.RefreshOnMiss(context.Background())
	}
	if len(source.calls) != 1 {
		t.Fatalf("plane read %d times inside one interval, want 1", len(source.calls))
	}
	clock = clock.Add(DefaultMissRefreshInterval)
	_ = store.RefreshOnMiss(context.Background())
	if len(source.calls) != 2 {
		t.Fatalf("plane read %d times after the interval passed, want 2", len(source.calls))
	}
}

func TestRefreshOnMissRepeatsTheLastFailureInsideTheInterval(t *testing.T) {
	down := errors.New("plane down")
	source := &stubSource{errs: []error{down}}
	store := New(source)
	store.now = func() time.Time { return time.Unix(1000, 0) }

	if err := store.RefreshOnMiss(context.Background()); !errors.Is(err, down) {
		t.Fatalf("first refresh = %v, want the plane's error", err)
	}
	if err := store.RefreshOnMiss(context.Background()); !errors.Is(err, down) {
		t.Fatalf("second refresh = %v, want the same error without a new read", err)
	}
	if len(source.calls) != 1 {
		t.Fatalf("plane read %d times, want 1", len(source.calls))
	}
}
