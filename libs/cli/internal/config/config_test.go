package config

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
)

func tempConfig(t *testing.T) string {
	t.Helper()
	return filepath.Join(t.TempDir(), "hangar", "config.yaml")
}

// TestDefaultPathHonoursTheOverridesInPrecedenceOrder. HANGAR_CONFIG is what a script or a CI job
// sets to keep out of the operator's real config; XDG is the fallback a desktop expects.
func TestDefaultPathHonoursTheOverridesInOrder(t *testing.T) {
	t.Setenv("HANGAR_CONFIG", "/explicit/config.yaml")
	t.Setenv("XDG_CONFIG_HOME", "/xdg")
	if got := DefaultPath(); got != "/explicit/config.yaml" {
		t.Fatalf("DefaultPath = %q, want the explicit override to win", got)
	}

	t.Setenv("HANGAR_CONFIG", "")
	if got := DefaultPath(); got != filepath.Join("/xdg", "hangar", "config.yaml") {
		t.Fatalf("DefaultPath = %q, want the XDG location", got)
	}

	t.Setenv("XDG_CONFIG_HOME", "")
	home, err := os.UserHomeDir()
	if err != nil {
		t.Skip("no home directory in this environment")
	}
	if got := DefaultPath(); got != filepath.Join(home, ".config", "hangar", "config.yaml") {
		t.Fatalf("DefaultPath = %q, want the home fallback", got)
	}
}

func TestStateDirHonoursTheOverridesInOrder(t *testing.T) {
	t.Setenv("HANGAR_STATE_DIR", "/explicit/state")
	t.Setenv("XDG_STATE_HOME", "/xdg")
	if got := StateDir(); got != "/explicit/state" {
		t.Fatalf("StateDir = %q, want the explicit override", got)
	}
	t.Setenv("HANGAR_STATE_DIR", "")
	if got := StateDir(); got != filepath.Join("/xdg", "hangar") {
		t.Fatalf("StateDir = %q, want the XDG location", got)
	}
}

// TestAMissingConfigIsAnEmptyDocumentNotAnError — a fresh checkout runs `hgr` with no config at
// all, and refusing to start would make the first run a setup task.
func TestAMissingConfigIsAnEmptyDocument(t *testing.T) {
	f, err := Load(filepath.Join(t.TempDir(), "nothing-here.yaml"))
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if f.CurrentContext != "default" {
		t.Errorf("current context = %q, want default", f.CurrentContext)
	}
	if f.Contexts == nil {
		t.Error("contexts is nil; every write path would panic")
	}
}

func TestMalformedYAMLIsRefusedWithThePathInTheMessage(t *testing.T) {
	path := tempConfig(t)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("contexts: [this is not a map\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := Load(path)
	if err == nil {
		t.Fatal("malformed YAML was accepted")
	}
	if !strings.Contains(err.Error(), path) {
		t.Errorf("the error does not say which file: %v", err)
	}
}

// TestSaveRoundTripsAndKeepsTheCredentialPrivate. The file holds a bearer token, so 0600 is not a
// nicety — a group-readable config leaks the operator's credential to every user on the box.
func TestSaveRoundTripsWithRestrictivePermissions(t *testing.T) {
	path := tempConfig(t)
	f, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	f.SetContext("prod", Context{Server: "https://plane.example", Token: "hgr_pat_secret", Project: "prj_1"})
	f.CurrentContext = "prod"
	f.Output = "json"
	if err := f.Save(); err != nil {
		t.Fatalf("save: %v", err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("config written with mode %o, want 0600", perm)
	}
	dir, err := os.Stat(filepath.Dir(path))
	if err != nil {
		t.Fatal(err)
	}
	if perm := dir.Mode().Perm(); perm != 0o700 {
		t.Errorf("config directory created with mode %o, want 0700", perm)
	}

	back, err := Load(path)
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	if back.CurrentContext != "prod" || back.Output != "json" {
		t.Errorf("round trip lost settings: %+v", back)
	}
	if got := back.Context(""); got.Server != "https://plane.example" || got.Token != "hgr_pat_secret" {
		t.Errorf("round trip lost the context: %+v", got)
	}
	if back.Path() != path {
		t.Errorf("Path() = %q, want %q", back.Path(), path)
	}
}

// TestAnUnknownContextIsEmptyRatherThanMissing — `hgr --context typo cluster ls` must fall through
// to a clear "no server configured", not panic on a nil map entry.
func TestAnUnknownContextIsEmpty(t *testing.T) {
	f, _ := Load(filepath.Join(t.TempDir(), "c.yaml"))
	if got := f.Context("nowhere"); got != (Context{}) {
		t.Errorf("unknown context = %+v, want the zero value", got)
	}
}

func TestContextNamesAreStable(t *testing.T) {
	f, _ := Load(filepath.Join(t.TempDir(), "c.yaml"))
	for _, name := range []string{"prod", "dev", "staging", "ap"} {
		f.SetContext(name, Context{})
	}
	want := []string{"ap", "dev", "prod", "staging"}
	for i := 0; i < 5; i++ {
		if got := f.ContextNames(); !reflect.DeepEqual(got, want) {
			t.Fatalf("ContextNames = %v, want %v", got, want)
		}
	}
}

// TestSetAndGetCoverEverySettableKey. The list is explicit so a typo is an error rather than a
// silently ignored setting; this holds the two halves to the same list.
func TestSetAndGetCoverEverySettableKey(t *testing.T) {
	f, _ := Load(filepath.Join(t.TempDir(), "c.yaml"))
	values := map[string]string{
		"current_context": "prod",
		"output":          "json",
		"fake":            "true",
		"context.server":  "https://plane.example",
		"context.token":   "hgr_pat_0123456789abcdef",
		"context.project": "prj_1",
		"context.user":    "operator",
	}
	for _, key := range Keys() {
		value, ok := values[key]
		if !ok {
			t.Fatalf("%q is settable but this test does not exercise it", key)
		}
		if err := f.Set(key, value); err != nil {
			t.Fatalf("set %s: %v", key, err)
		}
		got, err := f.Get(key)
		if err != nil {
			t.Fatalf("get %s: %v", key, err)
		}
		if key == "context.token" {
			if got == value {
				t.Error("the token was echoed back in full; `hgr config get` would print a credential")
			}
			continue
		}
		if got != value {
			t.Errorf("get %s = %q, want %q", key, got, value)
		}
	}
}

// TestSettingTheContextWritesToTheCurrentOne — `hgr config set context.server` with a non-default
// current context must not silently edit `default`.
func TestSettingTheContextWritesToTheCurrentOne(t *testing.T) {
	f, _ := Load(filepath.Join(t.TempDir(), "c.yaml"))
	if err := f.Set("current_context", "ap"); err != nil {
		t.Fatal(err)
	}
	if err := f.Set("context.server", "https://ap.example"); err != nil {
		t.Fatal(err)
	}
	if got := f.Contexts["ap"].Server; got != "https://ap.example" {
		t.Errorf("ap context server = %q", got)
	}
	if _, wrote := f.Contexts["default"]; wrote {
		t.Error("the default context was written to instead of the current one")
	}
}

func TestAnUnknownKeyIsRefused(t *testing.T) {
	f, _ := Load(filepath.Join(t.TempDir(), "c.yaml"))
	err := f.Set("context.servers", "x")
	if err == nil {
		t.Fatal("an unknown key was accepted")
	}
	var e *errs.Error
	if !errors.As(err, &e) {
		t.Fatalf("the refusal is not a structured error: %T", err)
	}
	if e.Code != errs.CodeUsage {
		t.Errorf("code = %v, want usage", e.Code)
	}
	if remediation(e) == "" {
		t.Fatal("an unknown key was refused with nowhere to go")
	}
	if !strings.Contains(remediation(e), "hgr config list") {
		t.Errorf("the refusal does not point at the key listing: %q", remediation(e))
	}
	if _, err := f.Get("nonsense"); err == nil {
		t.Error("Get accepted an unknown key")
	}
}

// TestASubstringOfARealKeyIsSuggested. The near-miss check is Contains(realKey, typed), so it
// fires for a partial key.
func TestASubstringOfARealKeyIsSuggested(t *testing.T) {
	f, _ := Load(filepath.Join(t.TempDir(), "c.yaml"))
	var e *errs.Error
	if !errors.As(f.Set("server", "x"), &e) {
		t.Fatal("not a structured error")
	}
	if !strings.Contains(remediation(e), "context.server") {
		t.Errorf("a partial key was not resolved to the real one: %q", remediation(e))
	}
}

// TestATypoWithAnExtraCharacterGetsNoSuggestion pins a real weakness rather than an intent: the
// near-miss check asks whether a settable key contains what was typed, so "context.servers" — the
// most common way to get this wrong — is longer than every candidate and matches none. The user
// still gets `hgr config list`, so nothing is broken, but the suggestion that would have saved the
// round trip does not appear.
//
// TODO: match on edit distance, or on the typed key's prefix, rather than containment.
func TestATypoWithAnExtraCharacterGetsNoSuggestion(t *testing.T) {
	f, _ := Load(filepath.Join(t.TempDir(), "c.yaml"))
	var e *errs.Error
	if !errors.As(f.Set("context.servers", "x"), &e) {
		t.Fatal("not a structured error")
	}
	if strings.Contains(remediation(e), "Did you mean") {
		t.Log("superstring typos are now suggested; delete the TODO above and tighten this test")
	}
}

// remediation flattens an error's next-step actions into something a test can assert on.
func remediation(e *errs.Error) string {
	var out strings.Builder
	for _, a := range e.Remediation {
		out.WriteString(a.Description)
		out.WriteString(" ")
		out.WriteString(a.Command)
		out.WriteString("\n")
	}
	return out.String()
}

func TestFakeMustBeABoolean(t *testing.T) {
	f, _ := Load(filepath.Join(t.TempDir(), "c.yaml"))
	if err := f.Set("fake", "maybe"); err == nil {
		t.Fatal("fake accepted a non-boolean")
	}
	if f.Fake {
		t.Error("a refused value was applied anyway")
	}
}

func TestUnsetClearsAKey(t *testing.T) {
	f, _ := Load(filepath.Join(t.TempDir(), "c.yaml"))
	if err := f.Set("context.token", "hgr_pat_secret"); err != nil {
		t.Fatal(err)
	}
	if err := f.Unset("context.token"); err != nil {
		t.Fatalf("unset: %v", err)
	}
	if f.Context("").Token != "" {
		t.Error("the token survived being unset")
	}
	if err := f.Unset("nonsense"); err == nil {
		t.Error("Unset accepted an unknown key")
	}
}

// TestRedactNeverReturnsTheSecret is the property that matters: whatever the length, the output
// must not be something that could be pasted back as a working credential.
func TestRedactNeverReturnsTheSecret(t *testing.T) {
	for _, secret := range []string{
		"", "a", "short", "tenchars12", "hgr_pat_0123456789abcdef", strings.Repeat("x", 200),
	} {
		got := Redact(secret)
		if secret != "" && got == secret {
			t.Errorf("Redact(%q) returned the input unchanged", secret)
		}
		if len(secret) > 10 && !strings.Contains(got, "********") {
			t.Errorf("Redact(%q) = %q, want a masked middle", secret, got)
		}
		if len(secret) <= 10 && strings.Trim(got, "*") != "" {
			t.Errorf("Redact(%q) = %q, want it fully masked", secret, got)
		}
	}
}

func TestEnvBoolAcceptsTheUsualSpellings(t *testing.T) {
	cases := map[string]bool{
		"1": true, "true": true, "TRUE": true, "yes": true, "on": true, " True ": true,
		"0": false, "false": false, "no": false, "off": false, "": false, "maybe": false,
	}
	for raw, want := range cases {
		t.Setenv("HANGAR_TEST_ENVBOOL", raw)
		if got := EnvBool("HANGAR_TEST_ENVBOOL"); got != want {
			t.Errorf("EnvBool(%q) = %v, want %v", raw, got, want)
		}
	}
}

// TestDescribeNeverPrintsACredential — context listings are the most likely thing to end up in a
// screenshot or a paste.
func TestDescribeNeverPrintsACredential(t *testing.T) {
	c := Context{Server: "https://plane.example", Token: "hgr_pat_0123456789abcdef", Project: "prj_1"}
	got := c.Describe()
	if strings.Contains(got, "0123456789abcdef") {
		t.Fatalf("Describe leaked the token: %q", got)
	}
	if !strings.Contains(got, "prj_1") || !strings.Contains(got, "https://plane.example") {
		t.Errorf("Describe = %q, want the server and project", got)
	}

	empty := Context{}.Describe()
	if !strings.Contains(empty, DefaultServer) {
		t.Errorf("an unconfigured context does not name the default server: %q", empty)
	}
	if !strings.Contains(empty, "no credentials") {
		t.Errorf("an unconfigured context does not say it has none: %q", empty)
	}
	if !strings.Contains(empty, "project=-") {
		t.Errorf("an absent project is not drawn as a dash: %q", empty)
	}
}
