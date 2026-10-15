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
// on: a rejection closes the dial and forgets the token, and anything else is a
// plane the gateway could not use, which falls back to what it already knows.
//
// 404 is deliberately on the unavailable side. A plane that has not shipped the
// route answers 404 for the path, and calling that a bad token sends everybody to
// look at Secrets during what is really a version skew. A 429 is the plane's
// failed-guess budget, a wait rather than a verdict. And a 401 or 403 on the
// gateway's own credential is a gateway misconfiguration: forgetting every
// cached agent token over it would turn a credential typo into an outage.
func TestOnlyAnAuthFailureIsARejection(t *testing.T) {
	cases := []struct {
		name       string
		status     int
		body       string
		credential int
		rejected   bool
		credFault  bool
	}{
		{name: "unknown enrolment token", status: http.StatusUnauthorized, credential: http.StatusNotModified, rejected: true},
		{name: "unknown enrolment token, first credential read", status: http.StatusUnauthorized, credential: http.StatusOK, rejected: true},
		{name: "gateway credential refused", status: http.StatusUnauthorized, credential: http.StatusUnauthorized, credFault: true},
		{name: "gateway principal not allowed", status: http.StatusForbidden, credential: http.StatusNotModified, credFault: true},
		{name: "credential check failed", status: http.StatusUnauthorized, credential: http.StatusBadGateway},
		{name: "throttled", status: http.StatusTooManyRequests, credential: http.StatusNotModified},
		{name: "route not shipped", status: http.StatusNotFound, credential: http.StatusNotModified},
		{name: "plane broken", status: http.StatusInternalServerError, credential: http.StatusNotModified},
		{name: "no cluster in the answer", status: http.StatusOK, body: `{}`, credential: http.StatusNotModified},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/v1/gateway/tenants" {
					w.WriteHeader(tc.credential)
					if tc.credential == http.StatusOK {
						_, _ = w.Write([]byte(`{"revision":"1","tenants":[]}`))
					}
					return
				}
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
			if errors.Is(err, ErrCredential) != tc.credFault {
				t.Fatalf("err = %v, credential fault should be %v", err, tc.credFault)
			}
		})
	}
}

// TestCheckCredentialIsAConditionalRead pins that the credential check reuses
// the last tenant revision, so a rejected dial costs the plane a 304 and not a
// full tenant table.
func TestCheckCredentialIsAConditionalRead(t *testing.T) {
	var seen []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Get("If-None-Match"))
		if r.Header.Get("If-None-Match") == "9" {
			w.WriteHeader(http.StatusNotModified)
			return
		}
		_, _ = w.Write([]byte(`{"revision":"9","tenants":[]}`))
	}))
	defer server.Close()

	client := New(server.URL, "gateway-token", time.Second)
	if _, err := client.TenantsResolved(context.Background(), ""); err != nil {
		t.Fatalf("tenants: %v", err)
	}
	if err := client.CheckCredential(context.Background()); err != nil {
		t.Fatalf("check: %v", err)
	}
	if len(seen) != 2 || seen[1] != "9" {
		t.Fatalf("If-None-Match sent = %q, want the check to send the last revision", seen)
	}
}

func TestACredentialRefusalOnTheTablesIsNamed(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer server.Close()

	client := New(server.URL, "gateway-token", time.Second)
	if _, err := client.Resolved(context.Background(), ""); !errors.Is(err, ErrCredential) {
		t.Fatalf("resolved err = %v, want ErrCredential", err)
	}
	if _, err := client.TenantsResolved(context.Background(), ""); !errors.Is(err, ErrCredential) {
		t.Fatalf("tenants err = %v, want ErrCredential", err)
	}
}
