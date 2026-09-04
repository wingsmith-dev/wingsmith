package render

import (
	"encoding/json"
	"strings"
	"testing"
)

// renderedJSON is the whole rendered graph as text, so a flag can be looked for on every
// container of every component at once.
func renderedJSON(t *testing.T, in Inputs) string {
	t.Helper()
	obj, err := DynamoGraphDeployment(in, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	raw, err := json.Marshal(obj.Object)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	return string(raw)
}

func contentLogging() map[string]string {
	return map[string]string{
		"--enable-log-requests": "",
		"enable-log-outputs":    "",
		"log-requests":          "",
		"log-requests-level":    "2",
		"--enable_log_outputs":  "",
		"max-num-seqs":          "64",
	}
}

func assertNoContentLogging(t *testing.T, rendered string) {
	t.Helper()
	for _, flag := range contentLoggingFlags {
		for _, spelling := range []string{flag, strings.ReplaceAll(flag, "-", "_")} {
			if strings.Contains(rendered, `"--`+spelling+`"`) {
				t.Errorf("--%s reached a worker's command line", spelling)
			}
		}
	}
	if !strings.Contains(rendered, `"--max-num-seqs"`) {
		t.Error("an ordinary engine arg beside them was dropped as well")
	}
}

func TestEngineArgsCannotTurnOnRequestContentLogging(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.EngineArgs = contentLogging()
	assertNoContentLogging(t, renderedJSON(t, in))
}

func TestARolesEngineArgsCannotTurnOnRequestContentLogging(t *testing.T) {
	in := disaggInputs()
	for i := range in.Deployment.Serving.Roles {
		in.Deployment.Serving.Roles[i].EngineArgs = contentLogging()
	}
	assertNoContentLogging(t, renderedJSON(t, in))
}

func TestTheOmniWorkerCannotTurnOnRequestContentLogging(t *testing.T) {
	in := omniInputs()
	in.Deployment.Serving.EngineArgs = contentLogging()
	assertNoContentLogging(t, renderedJSON(t, in))
}

// The negative spelling is what NVIDIA's own recipes carry, and it says the same thing the
// engine default does. Removing it would rename the worker for nothing.
func TestTheExplicitOffSpellingIsLeftAlone(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.EngineArgs = map[string]string{"no-enable-log-requests": ""}
	if !strings.Contains(renderedJSON(t, in), `"--no-enable-log-requests"`) {
		t.Error("--no-enable-log-requests was removed")
	}
}
