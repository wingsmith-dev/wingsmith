package declare

import (
	"os"
	"testing"

	lua "github.com/yuin/gopher-lua"
)

// Runs the Argo CD health check against sample statuses, the way argocd-cm does: obj is a
// global table and the script returns {status, message}.
func TestArgoCDHealthCheck(t *testing.T) {
	script, err := os.ReadFile("../../deploy/argocd/workload-health.lua")
	if err != nil {
		t.Fatal(err)
	}
	cond := func(typ, status, msg string) map[string]any {
		return map[string]any{"type": typ, "status": status, "message": msg}
	}
	cases := []struct {
		name   string
		obj    map[string]any
		status string
	}{
		{"no status", map[string]any{"metadata": map[string]any{"generation": 1}}, "Progressing"},
		{"stalled before observedGeneration", map[string]any{
			"metadata": map[string]any{"generation": 2},
			"status":   map[string]any{"conditions": []any{cond("Stalled", "True", "delete failing")}},
		}, "Degraded"},
		{"old generation", map[string]any{
			"metadata": map[string]any{"generation": 2},
			"status":   map[string]any{"observedGeneration": 1, "conditions": []any{cond("Ready", "True", "")}},
		}, "Progressing"},
		{"synced false", map[string]any{
			"metadata": map[string]any{"generation": 1},
			"status":   map[string]any{"observedGeneration": 1, "conditions": []any{cond("Synced", "False", "bad")}},
		}, "Degraded"},
		{"ready", map[string]any{
			"metadata": map[string]any{"generation": 1},
			"status":   map[string]any{"observedGeneration": 1, "conditions": []any{cond("Synced", "True", ""), cond("Ready", "True", "")}},
		}, "Healthy"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			L := lua.NewState()
			defer L.Close()
			L.SetGlobal("obj", toLua(L, tc.obj))
			if err := L.DoString(string(script)); err != nil {
				t.Fatal(err)
			}
			got := L.Get(-1).(*lua.LTable).RawGetString("status").String()
			if got != tc.status {
				t.Fatalf("status = %s, want %s", got, tc.status)
			}
		})
	}
}

func toLua(L *lua.LState, v any) lua.LValue {
	switch v := v.(type) {
	case map[string]any:
		t := L.NewTable()
		for k, e := range v {
			t.RawSetString(k, toLua(L, e))
		}
		return t
	case []any:
		t := L.NewTable()
		for _, e := range v {
			t.Append(toLua(L, e))
		}
		return t
	case string:
		return lua.LString(v)
	case int:
		return lua.LNumber(v)
	}
	return lua.LNil
}
