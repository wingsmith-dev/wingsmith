package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/errs"
)

// authPlane is a plane with the three auth routes and one guarded collection.
// It accepts exactly one access token at a time and rotates the refresh token
// on every use, the way the real one does.
type authPlane struct {
	mu        sync.Mutex
	access    string
	refresh   string
	revoked   []string
	refreshes int
	bodies    map[string]map[string]any
}

func (p *authPlane) handler(t *testing.T) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p.mu.Lock()
		defer p.mu.Unlock()
		var body map[string]any
		if raw, _ := io.ReadAll(r.Body); len(raw) > 0 {
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Errorf("%s: body is not JSON: %v", r.URL.Path, err)
			}
		}
		if p.bodies == nil {
			p.bodies = map[string]map[string]any{}
		}
		p.bodies[r.URL.Path] = body
		switch r.URL.Path {
		case "/v1/auth/login":
			if body["password"] != "pw" {
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = w.Write([]byte(`{"code":"not_authorized","message":"invalid credentials"}`))
				return
			}
			p.access, p.refresh = "access-1", "refresh-1"
			_, _ = w.Write([]byte(`{"access_token":"access-1","refresh_token":"refresh-1","expires_in":900,"kind":"admin"}`))
		case "/v1/auth/refresh":
			p.refreshes++
			if body["refresh_token"] != p.refresh {
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = w.Write([]byte(`{"code":"not_authorized","message":"invalid or expired refresh token"}`))
				return
			}
			p.access, p.refresh = "access-2", "refresh-2"
			_, _ = w.Write([]byte(`{"access_token":"access-2","refresh_token":"refresh-2","expires_in":900,"kind":"admin"}`))
		case "/v1/auth/logout":
			if rt, _ := body["refresh_token"].(string); rt != "" {
				p.revoked = append(p.revoked, rt)
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			if r.Header.Get("Authorization") != "Bearer "+p.access {
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = w.Write([]byte(`{"code":"not_authorized","message":"expired"}`))
				return
			}
			_, _ = w.Write([]byte(`{"items":[],"count":0}`))
		}
	}
}

func authClient(t *testing.T, p *authPlane, token, refresh string) *Client {
	t.Helper()
	srv := httptest.NewServer(p.handler(t))
	t.Cleanup(srv.Close)
	c, err := NewClient(srv.URL, token, "", "1.0", 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	c.RefreshToken = refresh
	return c
}

func TestLoginReturnsTheSessionPair(t *testing.T) {
	p := &authPlane{}
	c := authClient(t, p, "", "")
	res, err := c.Login(context.Background(), LoginRequest{Password: "pw", Slug: "admin"})
	if err != nil {
		t.Fatal(err)
	}
	if res.AccessToken != "access-1" || res.RefreshToken != "refresh-1" || res.Kind != "admin" {
		t.Errorf("login result = %+v", res)
	}
	if _, err := c.Login(context.Background(), LoginRequest{Password: "wrong"}); errs.As(err).Code != errs.CodeAuthRequired {
		t.Errorf("a wrong password: %v", err)
	}
}

// An expired access token is renewed once with the refresh token, the new pair
// is handed back to be stored, and the call that failed is retried.
func TestAnExpiredSessionIsRenewedOnceAndStored(t *testing.T) {
	p := &authPlane{access: "access-1", refresh: "refresh-1"}
	c := authClient(t, p, "access-0", "refresh-1")
	var stored LoginResult
	c.OnRefresh = func(r LoginResult) error { stored = r; return nil }

	if _, err := c.Clusters(context.Background(), ListOpts{}); err != nil {
		t.Fatalf("the call was not retried after renewing: %v", err)
	}
	if stored.AccessToken != "access-2" || stored.RefreshToken != "refresh-2" {
		t.Errorf("the rotated pair was not handed back to be stored: %+v", stored)
	}
	if c.Token != "access-2" || c.RefreshToken != "refresh-2" {
		t.Errorf("the client kept the old pair: %q %q", c.Token, c.RefreshToken)
	}
	if p.refreshes != 1 {
		t.Errorf("renewed %d times, want once", p.refreshes)
	}
}

// A refresh token the plane refuses ends the session: AUTH_EXPIRED, a pointer
// at login, and no loop.
func TestARefusedRefreshIsAuthExpired(t *testing.T) {
	p := &authPlane{access: "access-1", refresh: "refresh-1"}
	c := authClient(t, p, "access-0", "stale")
	_, err := c.Clusters(context.Background(), ListOpts{})
	e := errs.As(err)
	if e.Code != errs.CodeAuthExpired {
		t.Fatalf("code = %v, want AUTH_EXPIRED", e.Code)
	}
	if len(e.Remediation) == 0 || e.Remediation[0].Command != "wings login" {
		t.Errorf("remediation = %+v, want wings login", e.Remediation)
	}
	if p.refreshes != 1 {
		t.Errorf("tried to renew %d times, want once", p.refreshes)
	}
}

// Without a refresh token a 401 is what it was.
func TestNoRefreshTokenMeansNoRenewal(t *testing.T) {
	p := &authPlane{access: "access-1", refresh: "refresh-1"}
	c := authClient(t, p, "access-0", "")
	_, err := c.Clusters(context.Background(), ListOpts{})
	if errs.As(err).Code != errs.CodeAuthRequired || p.refreshes != 0 {
		t.Fatalf("code %v after %d renewals", errs.As(err).Code, p.refreshes)
	}
}

// Logout revokes the refresh token, which is the only thing the route reads.
func TestLogoutSendsTheRefreshToken(t *testing.T) {
	p := &authPlane{}
	c := authClient(t, p, "access-1", "")
	if err := c.Logout(context.Background(), "refresh-1"); err != nil {
		t.Fatal(err)
	}
	if got := p.bodies["/v1/auth/logout"]["refresh_token"]; got != "refresh-1" {
		t.Errorf("logout body = %v, want the refresh token", p.bodies["/v1/auth/logout"])
	}
	if len(p.revoked) != 1 || p.revoked[0] != "refresh-1" {
		t.Errorf("revoked = %v", p.revoked)
	}
}

// Interrupting the CLI is not a timeout: one is worth retrying and the other
// is the caller asking to stop.
func TestAnInterruptIsNotATimeout(t *testing.T) {
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	})
	ctx, cancel := context.WithCancel(context.Background())
	go func() { time.Sleep(20 * time.Millisecond); cancel() }()
	_, err := c.Clusters(ctx, ListOpts{})
	if got := errs.As(err).Code; got != errs.CodeInterrupted {
		t.Fatalf("code = %v, want INTERRUPTED", got)
	}
}

// The HTTP client's own timeout is a timeout, not an unreachable plane.
func TestTheClientTimeoutIsATimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	t.Cleanup(srv.Close)
	c, _ := NewClient(srv.URL, "", "", "1.0", 30*time.Millisecond)
	_, err := c.Clusters(context.Background(), ListOpts{})
	if got := errs.As(err).Code; got != errs.CodeTimeout {
		t.Fatalf("code = %v, want TIMEOUT", got)
	}
}
