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

func TestVerifyTunnelTokenAsksThePlaneAndAnswersWithTheCluster(t *testing.T) {
	var seen tunnelAuthRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != tunnelAuthPath {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer gateway-token" {
			t.Errorf("authorization = %q", r.Header.Get("Authorization"))
		}
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &seen)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"cluster_id":"clu_ap","cluster_name":"ap"}`))
	}))
	defer server.Close()

	identity, err := New(server.URL, "gateway-token", time.Second).VerifyTunnelToken(context.Background(), "enrol-abc")
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if identity.ClusterID != "clu_ap" {
		t.Fatalf("cluster = %q", identity.ClusterID)
	}
	// The name is carried too, and dropping it is not cosmetic: the listener
	// checks an agent's claim against it, and an agent claims the name.
	if identity.ClusterName != "ap" {
		t.Fatalf("cluster name = %q, want the plane's answer to reach the listener", identity.ClusterName)
	}
	if seen.Token != "enrol-abc" {
		t.Fatalf("the plane was sent %q", seen.Token)
	}
}

// TestOnlyAnAuthFailureIsARejection is the distinction the listener's cache turns
// on: a rejection closes the dial for good, and anything else is a plane the
// gateway could not reach, which falls back to what it already knows.
//
// 404 is deliberately on the unavailable side. A plane that has not shipped the
// route answers 404 for the path, and calling that a bad token sends everybody to
// look at Secrets during what is really a version skew.
func TestOnlyAnAuthFailureIsARejection(t *testing.T) {
	cases := []struct {
		name     string
		status   int
		body     string
		rejected bool
	}{
		{name: "unauthorized", status: http.StatusUnauthorized, rejected: true},
		{name: "forbidden", status: http.StatusForbidden, rejected: true},
		{name: "route not shipped", status: http.StatusNotFound},
		{name: "plane broken", status: http.StatusInternalServerError},
		{name: "no cluster in the answer", status: http.StatusOK, body: `{}`},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer server.Close()

			_, err := New(server.URL, "gateway-token", time.Second).VerifyTunnelToken(context.Background(), "enrol-abc")
			if err == nil {
				t.Fatal("expected a failure")
			}
			if errors.Is(err, tunnel.ErrRejected) != tc.rejected {
				t.Fatalf("err = %v, rejected should be %v", err, tc.rejected)
			}
		})
	}
}
