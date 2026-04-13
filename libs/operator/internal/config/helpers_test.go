package config

import (
	"log/slog"
	"reflect"
	"strings"
	"testing"
)

func TestParseLevelAcceptsTheNamesAndRejectsTheRest(t *testing.T) {
	cases := []struct {
		in   string
		want slog.Level
		ok   bool
	}{
		{"debug", slog.LevelDebug, true},
		{"DEBUG", slog.LevelDebug, true},
		{"  Info  ", slog.LevelInfo, true},
		{"", slog.LevelInfo, true},
		{"warn", slog.LevelWarn, true},
		{"warning", slog.LevelWarn, true},
		{"error", slog.LevelError, true},
		{"ERROR", slog.LevelError, true},
		{"trace", 0, false},
		{"verbose", 0, false},
		{"4", 0, false},
	}
	for _, c := range cases {
		got, err := parseLevel(c.in)
		if (err == nil) != c.ok {
			t.Errorf("parseLevel(%q) error = %v, want ok=%v", c.in, err, c.ok)
			continue
		}
		if c.ok && got != c.want {
			t.Errorf("parseLevel(%q) = %v, want %v", c.in, got, c.want)
		}
	}
}

// TestParseLevelNamesTheChoicesItWouldHaveAccepted — a level typo is a config error the operator
// has to be restarted to fix, so the message has to be enough to fix it on the first try.
func TestParseLevelNamesTheChoices(t *testing.T) {
	_, err := parseLevel("trace")
	if err == nil {
		t.Fatal("trace was accepted")
	}
	for _, want := range []string{"debug", "info", "warn", "error"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error does not offer %q: %v", want, err)
		}
	}
}

// TestEnvListToleratesHowAConfigMapIsActuallyWritten: across a line, with trailing commas, with
// space around each entry. A list that silently gained an empty allowed-operation would widen a
// read-only operator's permissions by a typo.
func TestEnvListToleratesHowAConfigMapIsWritten(t *testing.T) {
	cases := []struct {
		raw  string
		want []string
	}{
		{"", nil},
		{"   ", nil},
		{",", nil},
		{",,,", nil},
		{"cordon", []string{"cordon"}},
		{"cordon,drain", []string{"cordon", "drain"}},
		{" cordon , drain ", []string{"cordon", "drain"}},
		{"cordon,drain,", []string{"cordon", "drain"}},
		{"cordon,,drain", []string{"cordon", "drain"}},
		{"\n  cordon,\n  drain\n", []string{"cordon", "drain"}},
	}
	for _, c := range cases {
		t.Setenv("HANGAR_TEST_LIST", c.raw)
		if got := envList("HANGAR_TEST_LIST"); !reflect.DeepEqual(got, c.want) {
			t.Errorf("envList(%q) = %#v, want %#v", c.raw, got, c.want)
		}
	}
}

func TestEnvListIsNilWhenUnset(t *testing.T) {
	if got := envList("HANGAR_TEST_LIST_DEFINITELY_UNSET"); got != nil {
		t.Errorf("envList of an unset key = %#v, want nil", got)
	}
}

// TestEnvBoolFallsBackRatherThanFlipping is the one that matters for safety: HANGAR_READ_ONLY set
// to something unparseable must keep the default, not silently resolve to false and let an
// operator that was meant to be read-only start writing.
func TestEnvBoolFallsBackRatherThanFlipping(t *testing.T) {
	cases := []struct {
		raw  string
		def  bool
		want bool
	}{
		{"true", false, true},
		{"false", true, false},
		{"1", false, true},
		{"0", true, false},
		{"TRUE", false, true},
		{"False", true, false},
		{"t", false, true},
		{"", true, true},
		{"", false, false},
		{"yes", true, true},
		{"yes", false, false},
		{"maybe", true, true},
		{"maybe", false, false},
		{" true ", true, true},
		{" true ", false, false},
	}
	for _, c := range cases {
		t.Setenv("HANGAR_TEST_BOOL", c.raw)
		if got := envBool("HANGAR_TEST_BOOL", c.def); got != c.want {
			t.Errorf("envBool(%q, default %v) = %v, want %v", c.raw, c.def, got, c.want)
		}
	}
}

func TestEnvBoolKeepsTheDefaultWhenUnset(t *testing.T) {
	if !envBool("HANGAR_TEST_BOOL_DEFINITELY_UNSET", true) {
		t.Error("an unset key did not keep its default")
	}
	if envBool("HANGAR_TEST_BOOL_DEFINITELY_UNSET", false) {
		t.Error("an unset key did not keep its default")
	}
}

// TestEnvTreatsAnEmptyValueAsUnset — an env var set to the empty string is how a Helm template
// renders an omitted field, and it must mean "not configured" rather than "configured to nothing".
func TestEnvTreatsAnEmptyValueAsUnset(t *testing.T) {
	t.Setenv("HANGAR_TEST_STR", "")
	if got := env("HANGAR_TEST_STR", "fallback"); got != "fallback" {
		t.Errorf("env of an empty value = %q, want the default", got)
	}
	t.Setenv("HANGAR_TEST_STR", "set")
	if got := env("HANGAR_TEST_STR", "fallback"); got != "set" {
		t.Errorf("env = %q, want the value that was set", got)
	}
}

func TestEnvIntFallsBackOnAnythingItCannotRead(t *testing.T) {
	cases := []struct {
		raw  string
		def  int
		want int
	}{
		{"", 5, 5},
		{"7", 5, 7},
		{"0", 5, 0},
		{"-1", 5, -1},
		{"7.5", 5, 5},
		{"seven", 5, 5},
	}
	for _, c := range cases {
		t.Setenv("HANGAR_TEST_INT", c.raw)
		if got := envInt("HANGAR_TEST_INT", c.def); got != c.want {
			t.Errorf("envInt(%q, default %d) = %d, want %d", c.raw, c.def, got, c.want)
		}
	}
}
