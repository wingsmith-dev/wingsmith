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
