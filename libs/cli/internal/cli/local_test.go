package cli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fresh is a machine that has never run hgr: no config file, no server, no
// token, and the fake switched off.
func fresh(t *testing.T) string {
	t.Helper()
	for _, k := range []string{"HANGAR_SERVER", "HANGAR_TOKEN", "HANGAR_CONTEXT", "HANGAR_PROJECT", "HANGAR_OUTPUT", "HANGAR_ADMIN_PASSWORD"} {
		t.Setenv(k, "")
	}
	t.Setenv("HANGAR_FAKE", "0")
	return filepath.Join(t.TempDir(), "hangar", "config.yaml")
}

// The first commands in the README run before there is a token, and some
// before there is a server.
func TestLocalCommandsWorkOnAFreshInstall(t *testing.T) {
	cfg := fresh(t)
	for _, args := range [][]string{
		{"config", "set", "context.server", "http://localhost:8000"},
		{"config", "get", "context.server"},
		{"config", "ls"},
		{"context"},
		{"context", "ls"},
		{"explain"},
		{"explain", "NOT_FOUND"},
		{"version"},
		{"help"},
		{"--help"},
		{"completion", "bash"},
		{},
		{"cluster"},
	} {
		out, errOut, code := runWith(t, cfg, args...)
		if code != 0 {
			t.Errorf("hgr %s exited %d on a fresh install: %s %s", strings.Join(args, " "), code, out, errOut)
		}
	}
	got, _, _ := runWith(t, cfg, "config", "get", "context.server", "-o", "name")
	if strings.TrimSpace(got) != "http://localhost:8000" {
		t.Errorf("the server set on a fresh install did not stick: %q", got)
	}
}

// There is no compiled-in server. A command that needs one says so, and never
// reaches for a host nobody configured.
func TestNoServerIsAUsageErrorAndNeverADefault(t *testing.T) {
	cfg := fresh(t)
	t.Setenv("HANGAR_TOKEN", "hgrs1_test")
	_, errOut, code := runWith(t, cfg, "cluster", "ls")
	if code != 2 {
		t.Fatalf("cluster ls with no server exited %d, want 2 (USAGE): %s", code, errOut)
	}
	if !strings.Contains(errOut, "no control plane URL") || strings.Contains(errOut, "hangar.dev") {
		t.Errorf("the error does not say what is missing, or names a default: %s", errOut)
	}

	t.Setenv("HANGAR_TOKEN", "")
	t.Setenv("HANGAR_ADMIN_PASSWORD", "hunter2")
	_, errOut, code = runWith(t, cfg, "login", "--no-input")
	if code != 2 || !strings.Contains(errOut, "no control plane URL") {
		t.Fatalf("login with no server exited %d: %s", code, errOut)
	}
}

// A bad format in the file cannot lock the user out of the command that fixes
// it, and a bad one typed on the command line is refused before it is stored.
func TestABadStoredOutputDoesNotLockOutConfig(t *testing.T) {
	cfg := fresh(t)
	if err := os.MkdirAll(filepath.Dir(cfg), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cfg, []byte("output: nonsense\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HANGAR_FAKE", "1")
	_, errOut, code := runWith(t, cfg, "cluster", "ls")
	if code != 2 || !strings.Contains(errOut, "hgr config set output auto") {
		t.Errorf("a bad stored format exited %d without saying how to fix it: %s", code, errOut)
	}
	if _, errOut, code := runWith(t, cfg, "config", "set", "output", "json"); code != 0 {
		t.Fatalf("config set could not repair the format: %d %s", code, errOut)
	}
	if _, _, code := runWith(t, cfg, "cluster", "ls"); code != 0 {
		t.Errorf("cluster ls still fails after the repair: %d", code)
	}
	if _, _, code := runWith(t, cfg, "config", "set", "output", "nonsense"); code != 2 {
		t.Errorf("an invalid format was accepted: exit %d", code)
	}
}

// Every invocation mistake is USAGE, exit 2: an unknown command, an unknown
// subcommand of a noun, a wrong argument count, and an unknown flag.
func TestInvocationMistakesAreUsageErrors(t *testing.T) {
	for _, args := range [][]string{
		{"nonsense"},
		{"cluster", "nonsense"},
		{"cluster", "ls", "extra"},
		{"cluster", "ls", "--bogus"},
		{"cluster", "get"},
		{"config", "set", "output"},
	} {
		_, errOut, code := run(t, args...)
		if code != 2 {
			t.Errorf("hgr %s exited %d, want 2 (USAGE): %s", strings.Join(args, " "), code, errOut)
		}
		if !strings.Contains(errOut, "USAGE") {
			t.Errorf("hgr %s did not say USAGE: %s", strings.Join(args, " "), errOut)
		}
	}
	if _, _, code := run(t, "cluster"); code != 0 {
		t.Errorf("a bare noun prints its help and exits %d, want 0", code)
	}
}

// A suggestion rebuilt from argv never repeats a credential that was on it.
func TestRerunNeverEchoesACredential(t *testing.T) {
	cases := [][]string{
		{"cluster", "ls", "--token", "hgrs1_secret"},
		{"cluster", "ls", "--token=hgrs1_secret"},
		{"config", "set", "context.token", "hgrs1_secret"},
		{"config", "set", "context.refresh_token", "hgrs1_secret"},
		{"login", "--password", "hgrs1_secret"},
	}
	for _, args := range cases {
		got := redactArgs(args)
		joined := strings.Join(got, " ")
		if strings.Contains(joined, "hgrs1_secret") {
			t.Errorf("%v redacted to %q", args, joined)
		}
		if !strings.Contains(joined, redacted) {
			t.Errorf("%v: nothing marked as redacted: %q", args, joined)
		}
	}
	if got := strings.Join(redactArgs([]string{"cluster", "ls", "-o", "json"}), " "); got != "cluster ls -o json" {
		t.Errorf("an argv with no credential changed: %q", got)
	}

	cfg := fresh(t)
	t.Setenv("HANGAR_SERVER", "http://127.0.0.1:1")
	_, errOut, _ := runWith(t, cfg, "cluster", "ls", "--token", "")
	_, errOut2, _ := runWith(t, cfg, "--token=hgrs1_secret", "--server", "nope", "cluster", "ls")
	if strings.Contains(errOut+errOut2, "hgrs1_secret") {
		t.Errorf("an error suggestion echoed the token: %s %s", errOut, errOut2)
	}
}
