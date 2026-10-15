package plane

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// TestTheDialSourceIsSentToThePlane pins what the plane keys its failed-guess
// budget on. Without it every verification is charged to the gateway's own
// principal, and junk dials from anywhere spend the budget re-verification
// needs.
func TestTheDialSourceIsSentToThePlane(t *testing.T) {
	var seen tunnelAuthRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &seen)
		_, _ = w.Write([]byte(`{"cluster_id":"clu_ap","cluster_name":"ap"}`))
	}))
	defer server.Close()

	ctx := tunnel.WithDialSource(context.Background(), "203.0.113.7")
	if _, err := New(server.URL, "gateway-token", time.Second).VerifyTunnelToken(ctx, "enrol-abc"); err != nil {
		t.Fatalf("verify: %v", err)
	}
	if seen.Source != "203.0.113.7" {
		t.Fatalf("source = %q, want the dial's address", seen.Source)
	}
}

// TestAThrottledUnknownTokenIsARejection is the fail-open this closes. The
// plane answers 429 once a source has spent its budget, but it has looked the
// token up first and says so; a revoked token must still lose its tunnels
// while that source is throttled.
func TestAThrottledUnknownTokenIsARejection(t *testing.T) {
	for _, tc := range []struct {
		name     string
		body     string
		rejected bool
	}{
		{"unknown token", `{"code":"rate_limited","detail":{"verdict":"unknown_token","retry_after_seconds":60}}`, true},
		{"no verdict", `{"code":"rate_limited"}`, false},
		{"no body", ``, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != tunnelAuthPath {
					w.WriteHeader(http.StatusNotModified)
					return
				}
				w.WriteHeader(http.StatusTooManyRequests)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer server.Close()

			_, err := New(server.URL, "gateway-token", time.Second).VerifyTunnelToken(context.Background(), "enrol-abc")
			if err == nil {
				t.Fatal("a throttled answer verified a token")
			}
			if errors.Is(err, tunnel.ErrRejected) != tc.rejected {
				t.Fatalf("err = %v, rejected should be %v", err, tc.rejected)
			}
		})
	}
}
