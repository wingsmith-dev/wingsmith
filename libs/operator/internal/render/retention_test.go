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

func TestAVerboseEngineLogLevelIsRemovedAndAQuietOneKept(t *testing.T) {
	for _, tc := range []struct {
		level string
		kept  bool
	}{{"debug", false}, {"TRACE", false}, {"10", false}, {"info", true}, {"warning", true}} {
		in := testInputs()
		in.Deployment.Serving.EngineArgs = map[string]string{"--log_level": tc.level}
		rendered := renderedJSON(t, in)
		if got := strings.Contains(rendered, `"--log_level"`); got != tc.kept {
			t.Errorf("--log_level %s: kept=%v, want %v", tc.level, got, tc.kept)
		}
	}
}

func TestARolesVerboseLogLevelIsRemovedToo(t *testing.T) {
	in := disaggInputs()
	for i := range in.Deployment.Serving.Roles {
		in.Deployment.Serving.Roles[i].EngineArgs = map[string]string{"log-level": "debug"}
	}
	if strings.Contains(renderedJSON(t, in), `"--log-level"`) {
		t.Error("a role's --log-level debug reached a worker")
	}
}

// contentLoggingEnvOverlay sets every variable that logs request content, and one beside them that
// does not, through both overlays: the pod's env and a Dynamo component's envs.
func contentLoggingEnvOverlay(in *Inputs) {
	env := func() []any {
		return []any{
			map[string]any{"name": "DYN_LOG", "value": "info,dynamo_llm=trace"},
			map[string]any{"name": "DYN_LOGGING_CONFIG_PATH", "value": "/etc/dynamo/logging.toml"},
			map[string]any{"name": "VLLM_LOGGING_LEVEL", "value": "DEBUG"},
			map[string]any{"name": "VLLM_DEBUG_LOG_API_SERVER_RESPONSE", "value": "true"},
			map[string]any{"name": "DYN_LOG", "valueFrom": map[string]any{"configMapKeyRef": map[string]any{"name": "l", "key": "k"}}},
			map[string]any{"name": "NCCL_DEBUG", "value": "INFO"},
		}
	}
	in.Deployment.Serving.Overlay.PodSpec = map[string]any{
		"containers": []any{map[string]any{"name": "main", "env": env()}},
	}
	in.Deployment.Serving.Overlay.Dynamo = map[string]any{
		"extraComponentSettings": map[string]any{"envs": env()},
	}
}

func TestAnOverlayEnvVarThatLogsRequestContentNeverReachesAContainer(t *testing.T) {
	in := testInputs()
	contentLoggingEnvOverlay(&in)
	rendered := renderedJSON(t, in)
	for _, name := range []string{"DYN_LOG", "DYN_LOGGING_CONFIG_PATH", "VLLM_LOGGING_LEVEL", "VLLM_DEBUG_LOG_API_SERVER_RESPONSE"} {
		if strings.Contains(rendered, `"`+name+`"`) {
			t.Errorf("%s reached the rendered graph", name)
		}
	}
	if strings.Count(rendered, `"NCCL_DEBUG"`) != 2 {
		t.Error("an ordinary env var beside them was dropped as well")
	}
}

func TestAQuietLogLevelInAnOverlayIsKept(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.Overlay.PodSpec = map[string]any{
		"containers": []any{map[string]any{"name": "main", "env": []any{
			map[string]any{"name": "DYN_LOG", "value": "info,dynamo_llm=warn"},
			map[string]any{"name": "VLLM_LOGGING_LEVEL", "value": "WARNING"},
		}}},
	}
	rendered := renderedJSON(t, in)
	for _, name := range []string{"DYN_LOG", "VLLM_LOGGING_LEVEL"} {
		if !strings.Contains(rendered, `"`+name+`"`) {
			t.Errorf("%s at a quiet level was removed", name)
		}
	}
}

// The Dynamo overlay's lists are referenced, not copied, by the merge; the clamp must not edit the
// deployment it was handed.
func TestTheClampLeavesTheInputsOverlayAlone(t *testing.T) {
	in := testInputs()
	contentLoggingEnvOverlay(&in)
	renderedJSON(t, in)
	envs := in.Deployment.Serving.Overlay.Dynamo["extraComponentSettings"].(map[string]any)["envs"].([]any)
	if len(envs) != 6 {
		t.Errorf("the input overlay was edited: %d entries left", len(envs))
	}
}
