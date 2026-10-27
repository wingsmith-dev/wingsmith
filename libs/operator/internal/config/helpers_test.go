package config

import (
	"log/slog"
	"reflect"
	"strings"
	"testing"
	"time"
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

// TestParseLevelNamesTheChoicesItWouldHaveAccepted: a level typo is a config error the operator
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

// TestLoaderBoolRefusesWhatItCannotRead is the one that matters for safety: HANGAR_READ_ONLY set
// to something unparseable must stop the operator, not resolve to a default nobody chose.
func TestLoaderBoolRefusesWhatItCannotRead(t *testing.T) {
	cases := []struct {
		raw  string
		def  bool
		want bool
		ok   bool
	}{
		{"true", false, true, true},
		{"false", true, false, true},
		{"1", false, true, true},
		{"0", true, false, true},
		{"TRUE", false, true, true},
		{"False", true, false, true},
		{" true ", false, true, true},
		{"", true, true, true},
		{"", false, false, true},
		{"yes", false, false, false},
		{"maybe", true, true, false},
	}
	for _, c := range cases {
		t.Setenv("HANGAR_TEST_BOOL", c.raw)
		var l loader
		got := l.bool("HANGAR_TEST_BOOL", c.def)
		if (len(l.errs) == 0) != c.ok {
			t.Errorf("bool(%q) errors = %v, want ok=%v", c.raw, l.errs, c.ok)
		}
		if c.ok && got != c.want {
			t.Errorf("bool(%q, default %v) = %v, want %v", c.raw, c.def, got, c.want)
		}
	}
}

func TestLoaderBoolKeepsTheDefaultWhenUnset(t *testing.T) {
	var l loader
	if !l.bool("HANGAR_TEST_BOOL_DEFINITELY_UNSET", true) || l.bool("HANGAR_TEST_BOOL_DEFINITELY_UNSET", false) {
		t.Error("an unset key did not keep its default")
	}
	if len(l.errs) != 0 {
		t.Errorf("an unset key is not an error: %v", l.errs)
	}
}

func TestReadOnlyYesFailsStartup(t *testing.T) {
	t.Setenv("HANGAR_CLUSTER_ID", "cluster-1")
	t.Setenv("HANGAR_HUB_URL", "wss://hub.example/v1/hub/sync")
	t.Setenv("HANGAR_READ_ONLY", "yes")
	_, err := Load()
	if err == nil || !strings.Contains(err.Error(), "HANGAR_READ_ONLY") {
		t.Fatalf("error = %v, want one naming HANGAR_READ_ONLY", err)
	}
}

// TestEnvTreatsAnEmptyValueAsUnset: an env var set to the empty string is how a Helm template
// renders an omitted field, and it must mean "not configured" instead of "configured to nothing".
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

func TestLoaderIntAndDurationRefuseWhatTheyCannotRead(t *testing.T) {
	cases := []struct {
		raw  string
		want int
		ok   bool
	}{
		{"", 5, true},
		{"7", 7, true},
		{"0", 0, true},
		{"-1", -1, true},
		{"7.5", 0, false},
		{"seven", 0, false},
	}
	for _, c := range cases {
		t.Setenv("HANGAR_TEST_INT", c.raw)
		var l loader
		got := l.int("HANGAR_TEST_INT", 5)
		if (len(l.errs) == 0) != c.ok {
			t.Errorf("int(%q) errors = %v, want ok=%v", c.raw, l.errs, c.ok)
		}
		if c.ok && got != c.want {
			t.Errorf("int(%q) = %d, want %d", c.raw, got, c.want)
		}
	}

	t.Setenv("HANGAR_TEST_DURATION", "30")
	var l loader
	l.duration("HANGAR_TEST_DURATION", time.Second)
	if len(l.errs) != 1 {
		t.Errorf("a duration without a unit was accepted: %v", l.errs)
	}
}
