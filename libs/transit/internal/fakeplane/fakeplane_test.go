package fakeplane

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func call(t *testing.T, h http.Handler, method, path, token, revision string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var payload bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&payload).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	req := httptest.NewRequest(method, path, &payload)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if revision != "" {
		req.Header.Set("If-None-Match", revision)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestTheTenantTableIsServedEmptyAndStable(t *testing.T) {
	plane := New(Config{ServiceToken: "svc"})
	h := plane.Handler()

	rec := call(t, h, http.MethodGet, "/v1/gateway/tenants", "svc", "", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var table TenantTable
	if err := json.NewDecoder(rec.Body).Decode(&table); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if table.Revision == "" || table.Tenants == nil || len(table.Tenants) != 0 {
		t.Fatalf("table = %+v, want a revision and an empty, non-null tenant list", table)
	}
	if got := rec.Header().Get("ETag"); got != table.Revision {
		t.Fatalf("ETag = %q, want the revision %q", got, table.Revision)
	}

	if rec := call(t, h, http.MethodGet, "/v1/gateway/tenants", "svc", table.Revision, nil); rec.Code != http.StatusNotModified {
		t.Fatalf("status at the same revision = %d, want 304", rec.Code)
	}
	if rec := call(t, h, http.MethodGet, "/v1/gateway/tenants", "wrong", "", nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("status with the wrong token = %d, want 401", rec.Code)
	}
	if got := plane.TenantReads(); got != 2 {
		t.Fatalf("tenant reads = %d, want 2 (a refused read is not one)", got)
	}
}

func TestAConfiguredTenantTableIsServed(t *testing.T) {
	h := New(Config{Tenants: TenantTable{
		Revision: "t-7",
		Tenants:  []GatewayTenant{{ID: "ten_1", Slug: "acme", Enabled: true, KeyHashes: []string{"abc"}}},
	}}).Handler()

	rec := call(t, h, http.MethodGet, "/v1/gateway/tenants", "", "", nil)
	var table TenantTable
	if err := json.NewDecoder(rec.Body).Decode(&table); err != nil {
		t.Fatal(err)
	}
	if table.Revision != "t-7" || len(table.Tenants) != 1 || table.Tenants[0].Slug != "acme" {
		t.Fatalf("table = %+v", table)
	}
}

func TestTheRoutingTableIsConditional(t *testing.T) {
	plane := New(Config{ServiceToken: "svc", Table: Table{Revision: "r-1", Endpoints: []Endpoint{{ID: "e", Name: "m"}}}})
	h := plane.Handler()

	if rec := call(t, h, http.MethodGet, "/v1/endpoints/resolved", "", "", nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("no bearer = %d, want 401", rec.Code)
	}
	rec := call(t, h, http.MethodGet, "/v1/endpoints/resolved", "svc", "", nil)
	if rec.Code != http.StatusOK || rec.Header().Get("ETag") != "r-1" {
		t.Fatalf("status %d etag %q", rec.Code, rec.Header().Get("ETag"))
	}
	if rec := call(t, h, http.MethodGet, "/v1/endpoints/resolved", "svc", "r-1", nil); rec.Code != http.StatusNotModified {
		t.Fatalf("same revision = %d, want 304", rec.Code)
	}
	if resolved, _, _ := plane.Counts(); resolved != 2 {
		t.Fatalf("resolved = %d, want 2", resolved)
	}
}

func TestTunnelAuthResolvesOnlyKnownTokens(t *testing.T) {
	plane := New(Config{
		ServiceToken: "svc",
		Clusters:     map[string]Cluster{"enrol": {ID: "clu_1", Name: "one"}},
	})
	h := plane.Handler()

	rec := call(t, h, http.MethodPost, "/v1/clusters/tunnel-auth", "svc", "", map[string]string{"token": "enrol"})
	var cluster Cluster
	if err := json.NewDecoder(rec.Body).Decode(&cluster); err != nil || rec.Code != http.StatusOK {
		t.Fatalf("status %d err %v", rec.Code, err)
	}
	if cluster.ID != "clu_1" || cluster.Name != "one" {
		t.Fatalf("cluster = %+v", cluster)
	}
	if rec := call(t, h, http.MethodPost, "/v1/clusters/tunnel-auth", "svc", "", map[string]string{"token": "guess"}); rec.Code != http.StatusUnauthorized {
		t.Fatalf("unknown token = %d, want 401", rec.Code)
	}
	if rec := call(t, h, http.MethodPost, "/v1/clusters/tunnel-auth", "enrol", "", map[string]string{"token": "enrol"}); rec.Code != http.StatusUnauthorized {
		t.Fatalf("an enrolment token as the bearer = %d, want 401", rec.Code)
	}
	if _, verified, refused := plane.Counts(); verified != 1 || refused != 1 {
		t.Fatalf("verified %d refused %d, want 1 and 1", verified, refused)
	}
}

func TestOnlyTheGatewaysRoutesAreServed(t *testing.T) {
	h := New(Config{}).Handler()
	for _, path := range []string{"/v1/endpoints", "/v1/clusters", "/v1/serving/models"} {
		if rec := call(t, h, http.MethodGet, path, "", "", nil); rec.Code != http.StatusNotFound {
			t.Fatalf("GET %s = %d, want 404", path, rec.Code)
		}
	}
}
