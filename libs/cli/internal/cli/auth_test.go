package cli

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/hangar-dev/hangar/libs/cli/internal/config"
)

// testPlane records what each auth route was sent.
type testPlane struct {
	mu     sync.Mutex
	bodies map[string][]map[string]any
	hits   int
}

func (p *testPlane) serve(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.mu.Lock()
		defer p.mu.Unlock()
		p.hits++
		var body map[string]any
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		if p.bodies == nil {
			p.bodies = map[string][]map[string]any{}
		}
		p.bodies[r.URL.Path] = append(p.bodies[r.URL.Path], body)
		switch r.URL.Path {
		case "/v1/auth/login":
			if body["password"] != "s3cret" {
				w.WriteHeader(http.StatusUnauthorized)
				_, _ = w.Write([]byte(`{"code":"not_authorized","message":"invalid credentials"}`))
				return
			}
			_, _ = w.Write([]byte(`{"access_token":"hgrs1_access","refresh_token":"hgrr1_refresh","expires_in":900,"kind":"admin"}`))
		case "/v1/auth/logout":
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// withStdin feeds the CLI's stdin for one run.
func withStdin(t *testing.T, input string) {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	_, _ = w.WriteString(input)
	w.Close()
	old := os.Stdin
	os.Stdin = r
	t.Cleanup(func() { os.Stdin = old; r.Close() })
}

func TestLoginStoresTheSessionAndTheServerItUsed(t *testing.T) {
	cfg := fresh(t)
	p := &testPlane{}
	srv := p.serve(t)

	withStdin(t, "s3cret\n")
	out, errOut, code := runWith(t, cfg, "login", "--server", srv.URL, "--password-stdin", "-o", "json")
	if code != 0 {
		t.Fatalf("login exit %d: %s %s", code, out, errOut)
	}
	if strings.Contains(out+errOut, "hgrs1_access") || strings.Contains(out+errOut, "hgrr1_refresh") {
		t.Fatal("login printed a token")
	}
	if got := p.bodies["/v1/auth/login"][0]["password"]; got != "s3cret" {
		t.Errorf("password sent = %v, want the line from stdin without its newline", got)
	}
	stored, err := config.Load(cfg)
	if err != nil {
		t.Fatal(err)
	}
	c := stored.Context("")
	if c.Server != srv.URL || c.Token != "hgrs1_access" || c.RefreshToken != "hgrr1_refresh" {
		t.Errorf("stored context = %+v, want the server, access and refresh tokens", c)
	}
	if info, err := os.Stat(cfg); err != nil || info.Mode().Perm() != 0o600 {
		t.Errorf("config holding a session is not 0600: %v", info.Mode().Perm())
	}
}

// The password is never an argument: it would sit in shell history and in the
// process table for anyone on the machine to read.
func TestLoginRefusesAPasswordOnTheCommandLine(t *testing.T) {
	cfg := fresh(t)
	p := &testPlane{}
	srv := p.serve(t)
	if _, _, code := runWith(t, cfg, "login", "s3cret", "--server", srv.URL); code != 2 {
		t.Errorf("a positional password exited %d, want 2 (USAGE)", code)
	}
	if _, _, code := runWith(t, cfg, "login", "--password", "s3cret", "--server", srv.URL); code != 2 {
		t.Errorf("--password exited %d, want 2 (USAGE)", code)
	}
	if p.hits != 0 {
		t.Errorf("the plane was called %d times with a refused invocation", p.hits)
	}
}

func TestLoginWithNoPasswordAndNoTerminalIsInputRequired(t *testing.T) {
	cfg := fresh(t)
	p := &testPlane{}
	srv := p.serve(t)
	_, errOut, code := runWith(t, cfg, "login", "--server", srv.URL, "--no-input")
	if code != 2 || !strings.Contains(errOut, "--password-stdin") {
		t.Fatalf("exit %d: %s", code, errOut)
	}
}

func TestLogoutRevokesTheRefreshTokenAndClearsBoth(t *testing.T) {
	cfg := fresh(t)
	p := &testPlane{}
	srv := p.serve(t)
	t.Setenv("HANGAR_ADMIN_PASSWORD", "s3cret")
	if _, errOut, code := runWith(t, cfg, "login", "--server", srv.URL, "--no-input"); code != 0 {
		t.Fatalf("login: %d %s", code, errOut)
	}

	out, errOut, code := runWith(t, cfg, "logout", "-o", "json")
	if code != 0 {
		t.Fatalf("logout exit %d: %s", code, errOut)
	}
	if got := p.bodies["/v1/auth/logout"]; len(got) != 1 || got[0]["refresh_token"] != "hgrr1_refresh" {
		t.Fatalf("logout sent %v, want the stored refresh token", got)
	}
	var data map[string]any
	if err := json.Unmarshal([]byte(out), &data); err != nil || data["revoked"] != true {
		t.Errorf("logout result = %s", out)
	}
	stored, _ := config.Load(cfg)
	if c := stored.Context(""); c.Token != "" || c.RefreshToken != "" || c.Server != srv.URL {
		t.Errorf("after logout the context is %+v, want no tokens and the server kept", c)
	}
}

// A plane that cannot be reached still gets the local copy cleared, and the
// caller is told the session was not revoked.
func TestLogoutSaysSoWhenItCouldNotRevoke(t *testing.T) {
	cfg := fresh(t)
	p := &testPlane{}
	srv := p.serve(t)
	t.Setenv("HANGAR_ADMIN_PASSWORD", "s3cret")
	if _, _, code := runWith(t, cfg, "login", "--server", srv.URL, "--no-input"); code != 0 {
		t.Fatal("login failed")
	}
	srv.Close()

	_, errOut, code := runWith(t, cfg, "logout", "-o", "human")
	if code != 0 {
		t.Fatalf("logout exit %d: %s", code, errOut)
	}
	if !strings.Contains(errOut, "not revoked") {
		t.Errorf("a failed revocation was silent: %s", errOut)
	}
	stored, _ := config.Load(cfg)
	if c := stored.Context(""); c.Token != "" || c.RefreshToken != "" {
		t.Errorf("the local session survived: %+v", c)
	}
}

// --fake is sealed: login and logout reach no network and write nothing, and
// whoami reports the token the context really has.
func TestTheFakeIsSealed(t *testing.T) {
	cfg := fresh(t)
	t.Setenv("HANGAR_FAKE", "1")
	t.Setenv("HANGAR_SERVER", "http://127.0.0.1:1")
	t.Setenv("HANGAR_ADMIN_PASSWORD", "anything")
	if _, errOut, code := runWith(t, cfg, "login", "--no-input"); code != 0 {
		t.Fatalf("fake login exit %d: %s", code, errOut)
	}
	if _, err := os.Stat(cfg); !os.IsNotExist(err) {
		t.Error("a fake login wrote the config file")
	}
	if _, errOut, code := runWith(t, cfg, "logout"); code != 0 {
		t.Fatalf("fake logout exit %d: %s", code, errOut)
	}

	out, _, code := runWith(t, cfg, "whoami", "-o", "json")
	if code != 0 {
		t.Fatalf("whoami exit %d", code)
	}
	var id map[string]any
	_ = json.Unmarshal([]byte(out), &id)
	if id["has_token"] != false {
		t.Errorf("the fake claims a token the context does not have: %s", out)
	}
}

// --context picks a context for one command. A command that writes the file,
// here logout, must not leave that choice behind as the new default.
func TestAContextOverrideIsNotSavedAsTheDefault(t *testing.T) {
	cfg := fresh(t)
	p := &testPlane{}
	srv := p.serve(t)
	f, _ := config.Load(cfg)
	f.SetContext("default", config.Context{Server: srv.URL})
	f.SetContext("other", config.Context{Server: srv.URL, Token: "hgrs1_other", RefreshToken: "hgrr1_other"})
	if err := f.Save(); err != nil {
		t.Fatal(err)
	}

	if _, errOut, code := runWith(t, cfg, "--context", "other", "logout", "-o", "json"); code != 0 {
		t.Fatalf("logout exit %d: %s", code, errOut)
	}
	stored, _ := config.Load(cfg)
	if stored.CurrentContext != "default" {
		t.Errorf("current_context = %q after a --context other logout, want default kept", stored.CurrentContext)
	}
	if c := stored.Contexts["other"]; c.Token != "" || c.RefreshToken != "" {
		t.Errorf("logout cleared the wrong context: other is %+v", c)
	}
}
