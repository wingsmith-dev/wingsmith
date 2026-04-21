package plane

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestResolvedSendsTheRevisionBackAndReadsA304AsNotModified(t *testing.T) {
	var seen string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Get("If-None-Match")
		if r.Header.Get("Authorization") != "Bearer token" {
			t.Errorf("authorization = %q", r.Header.Get("Authorization"))
		}
		if seen == "17" {
			w.WriteHeader(http.StatusNotModified)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"revision":"17","endpoints":[{"name":"chat","upstreams":[{"url":"http://ap.svc:8000","weight":100}]}]}`))
	}))
	defer server.Close()

	client := New(server.URL, "token", time.Second)

	table, err := client.Resolved(context.Background(), "")
	if err != nil {
		t.Fatalf("first fetch: %v", err)
	}
	if table.Revision != "17" || len(table.Endpoints) != 1 {
		t.Fatalf("table = %+v", table)
	}
	if table.Endpoints[0].Upstreams[0].URL != "http://ap.svc:8000" {
		t.Fatalf("upstream = %+v", table.Endpoints[0].Upstreams[0])
	}

	if _, err := client.Resolved(context.Background(), "17"); !errors.Is(err, ErrNotModified) {
		t.Fatalf("second fetch = %v, want ErrNotModified", err)
	}
}

func TestAnErrorStatusIsReportedRatherThanDecoded(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	if _, err := New(server.URL, "token", time.Second).Resolved(context.Background(), ""); err == nil {
		t.Fatal("expected a 500 to be an error")
	}
}
