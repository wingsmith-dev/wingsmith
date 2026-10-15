package cli

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// recorded is one request a command sent to the plane.
type recorded struct {
	method, path, idempotencyKey string
	body                         map[string]any
}

// fixturePlane answers from the API fixtures and records every write, so a
// command's request can be checked against what the plane expects.
func fixturePlane(t *testing.T) (*httptest.Server, func() []recorded) {
	t.Helper()
	var mu sync.Mutex
	var writes []recorded
	items := func(name string) []map[string]any {
		raw, err := os.ReadFile(filepath.Join("..", "api", "testdata", name))
		if err != nil {
			t.Fatal(err)
		}
		var page struct {
			Items []map[string]any `json:"items"`
		}
		if err := json.Unmarshal(raw, &page); err != nil {
			t.Fatal(err)
		}
		return page.Items
	}
	collections := map[string][]map[string]any{
		"workloads":  items("workloads.json"),
		"nodes":      items("nodes.json"),
		"operations": items("operations.json"),
		"clusters":   items("clusters.json"),
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			raw, _ := io.ReadAll(r.Body)
			var body map[string]any
			_ = json.Unmarshal(raw, &body)
			mu.Lock()
			writes = append(writes, recorded{r.Method, r.URL.Path, r.Header.Get("Idempotency-Key"), body})
			mu.Unlock()
		}
		parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
		if len(parts) < 2 || parts[0] != "v1" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		rows := collections[parts[1]]
		switch {
		case r.Method == http.MethodPost && parts[1] == "operations":
			_ = json.NewEncoder(w).Encode(rows[0])
		case r.Method == http.MethodDelete:
			w.WriteHeader(http.StatusNoContent)
		case len(parts) == 2:
			_ = json.NewEncoder(w).Encode(map[string]any{"items": rows, "count": len(rows), "next_cursor": nil})
		default:
			for _, row := range rows {
				if row["id"] == parts[2] || row["name"] == parts[2] {
					_ = json.NewEncoder(w).Encode(row)
					return
				}
			}
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"code":"not_found","message":"not found","detail":{}}`))
		}
	}))
	t.Cleanup(srv.Close)
	return srv, func() []recorded {
		mu.Lock()
		defer mu.Unlock()
		return append([]recorded(nil), writes...)
	}
}

// Each write verb sends the request the plane expects: the right method and
// path, the operation type and target, the parameters the plane reads, and an
// Idempotency-Key on every operation.
func TestWriteVerbsSendWhatThePlaneReads(t *testing.T) {
	const wkl = "wkl_01m0dsfegh7fk4meb5jqnptc3c"
	cases := []struct {
		args   []string
		method string
		path   string
		body   map[string]any
	}{
		{[]string{"workload", "scale", "llama-70b-prod", "3"}, "POST", "/v1/operations",
			map[string]any{"type": "workload.scale", "target": wkl, "parameters": map[string]any{"desired": float64(3)}}},
		{[]string{"workload", "pause", "llama-70b-prod"}, "POST", "/v1/operations",
			map[string]any{"type": "workload.pause", "target": wkl, "parameters": map[string]any{"paused": true}}},
		{[]string{"workload", "resume", "llama-70b-prod"}, "POST", "/v1/operations",
			map[string]any{"type": "workload.pause", "target": wkl, "parameters": map[string]any{"paused": false}}},
		{[]string{"operation", "create", "node.drain", "iad-1-n01", "--reason", "firmware"}, "POST", "/v1/operations",
			map[string]any{"type": "node.drain", "target": "node:iad-1-n01", "reason": "firmware"}},
		{[]string{"operation", "create", "node.cordon", "iad-1-n01"}, "POST", "/v1/operations",
			map[string]any{"type": "node.cordon", "target": "node:iad-1-n01"}},
		{[]string{"workload", "rm", "llama-70b-prod"}, "DELETE", "/v1/workloads/" + wkl, nil},
	}
	for _, c := range cases {
		t.Run(strings.Join(c.args, " "), func(t *testing.T) {
			srv, writes := fixturePlane(t)
			t.Setenv("HANGAR_FAKE", "0")
			t.Setenv("HANGAR_SERVER", srv.URL)
			t.Setenv("HANGAR_TOKEN", "test-token")
			out, errOut, code := run(t, append(c.args, "--yes", "-o", "json")...)
			if code != 0 {
				t.Fatalf("exit %d\n%s%s", code, out, errOut)
			}
			got := writes()
			if len(got) != 1 {
				t.Fatalf("sent %d writes, want 1: %+v", len(got), got)
			}
			w := got[0]
			if w.method != c.method || w.path != c.path {
				t.Errorf("sent %s %s, want %s %s", w.method, w.path, c.method, c.path)
			}
			if c.path == "/v1/operations" && w.idempotencyKey == "" {
				t.Error("an operation was created without an Idempotency-Key")
			}
			for k, want := range c.body {
				gotJSON, _ := json.Marshal(w.body[k])
				wantJSON, _ := json.Marshal(want)
				if string(gotJSON) != string(wantJSON) {
					t.Errorf("body %s = %s, want %s", k, gotJSON, wantJSON)
				}
			}
		})
	}
}
