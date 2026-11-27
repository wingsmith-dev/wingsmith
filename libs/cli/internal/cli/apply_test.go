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

// manifestPlane is a plane holding manifests by path. PUT stores the body (not
// on a dry run) and answers with X-Wingsmith-Result; a name starting "bad"
// is refused with a 422.
type manifestPlane struct {
	mu      sync.Mutex
	objects map[string]map[string]any
	puts    []string
}

func (p *manifestPlane) serve(t *testing.T) {
	t.Helper()
	p.objects = map[string]map[string]any{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.mu.Lock()
		defer p.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		live, ok := p.objects[r.URL.Path]
		switch r.Method {
		case http.MethodGet:
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				_, _ = w.Write([]byte(`{"code":"not_found","message":"no such object"}`))
				return
			}
			_ = json.NewEncoder(w).Encode(live)
		case http.MethodPut:
			dry := r.URL.Query().Get("dryRun") == "All"
			if !dry {
				p.puts = append(p.puts, r.URL.Path)
			}
			if strings.HasPrefix(filepath.Base(r.URL.Path), "bad") {
				w.WriteHeader(http.StatusUnprocessableEntity)
				_, _ = w.Write([]byte(`{"code":"invalid","message":"spec.cluster cannot change after create"}`))
				return
			}
			raw, _ := io.ReadAll(r.Body)
			var body map[string]any
			_ = json.Unmarshal(raw, &body)
			body["status"] = map[string]any{"phase": "running"}
			meta := body["metadata"].(map[string]any)
			meta["uid"], meta["generation"] = "wl_1", 1
			result := "created"
			if ok {
				result = "configured"
				if a, b := mustJSON(live["spec"]), mustJSON(body["spec"]); a == b {
					result = "unchanged"
				}
			}
			if !dry {
				p.objects[r.URL.Path] = body
			}
			w.Header().Set("X-Wingsmith-Result", result)
			if result == "created" {
				w.WriteHeader(http.StatusCreated)
			}
			_ = json.NewEncoder(w).Encode(body)
		}
	}))
	t.Cleanup(srv.Close)
	t.Setenv("WINGSMITH_FAKE", "0")
	t.Setenv("WINGSMITH_SERVER", srv.URL)
	t.Setenv("WINGSMITH_TOKEN", "wss1_test")
	t.Setenv("WINGSMITH_PROJECT", "core")
}

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func doc(kind, name, spec string) string {
	return "apiVersion: plane.wingsmith.dev/v1alpha1\nkind: " + kind + "\nmetadata: {name: " + name + "}\nspec: " + spec + "\n"
}

func TestApplyReadsADirectoryAndRecursesOnlyWithR(t *testing.T) {
	p := &manifestPlane{}
	p.serve(t)
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "b.yaml"), doc("Endpoint", "chat", "{}"))
	writeFile(t, filepath.Join(dir, "a.yaml"), doc("Workload", "one", "{}")+"---\n"+doc("Workload", "two", "{}"))
	writeFile(t, filepath.Join(dir, "notes.txt"), "not a manifest")
	writeFile(t, filepath.Join(dir, "sub", "c.yaml"), doc("Workload", "three", "{}"))

	out, errOut, code := run(t, "apply", "-f", dir, "-o", "human")
	if code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	if strings.Contains(out, "three") {
		t.Errorf("a subdirectory without -R was read:\n%s", out)
	}
	want := []string{"/v1/manifests/workloads/core/one", "/v1/manifests/workloads/core/two", "/v1/manifests/endpoints/core/chat"}
	if strings.Join(p.puts, " ") != strings.Join(want, " ") {
		t.Errorf("order %v, want %v", p.puts, want)
	}
	if !strings.Contains(out, "workload/one created") {
		t.Errorf("missing result:\n%s", out)
	}

	p.puts = nil
	out, errOut, code = run(t, "apply", "-f", dir, "-R", "-o", "human")
	if code != 0 {
		t.Fatalf("-R: exit %d: %s", code, errOut)
	}
	for _, want := range []string{"workload/one unchanged", "workload/three created", "endpoint/chat unchanged"} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in\n%s", want, out)
		}
	}
	if got := p.puts[len(p.puts)-1]; got != "/v1/manifests/endpoints/core/chat" {
		t.Errorf("endpoints go last, got %v", p.puts)
	}
}

func TestApplyContinuesPastAFailedDocumentAndExitsOne(t *testing.T) {
	p := &manifestPlane{}
	p.serve(t)
	file := filepath.Join(t.TempDir(), "m.yaml")
	writeFile(t, file, doc("Workload", "bad", "{}")+"---\n"+doc("Workload", "good", "{}")+"---\nkind: Gadget\nmetadata: {name: x}\n")

	out, errOut, code := run(t, "apply", "-f", file, "-o", "human")
	if code != 1 {
		t.Fatalf("exit %d, want 1: %s", code, errOut)
	}
	if !strings.Contains(out, "workload/good created") {
		t.Errorf("the good document was not applied:\n%s", out)
	}
	for _, want := range []string{"workload/bad failed: spec.cluster cannot change", "gadget/x failed: document 3 has unknown kind"} {
		if !strings.Contains(errOut, want) {
			t.Errorf("missing %q in\n%s", want, errOut)
		}
	}
}

func TestDiffExitsZeroOneOrAboveOne(t *testing.T) {
	p := &manifestPlane{}
	p.serve(t)
	file := filepath.Join(t.TempDir(), "m.yaml")
	writeFile(t, file, doc("Workload", "chat", "{replicas: 2}"))

	out, errOut, code := run(t, "diff", "-f", file)
	if code != 1 || !strings.Contains(out, "+spec:") || !strings.Contains(out, "--- live/workload/chat") {
		t.Fatalf("new object: exit %d\n%s%s", code, out, errOut)
	}
	if len(p.puts) != 0 {
		t.Fatalf("diff applied something: %v", p.puts)
	}
	if _, errOut, code := run(t, "apply", "-f", file); code != 0 {
		t.Fatalf("apply: exit %d: %s", code, errOut)
	}
	out, errOut, code = run(t, "diff", "-f", file)
	if code != 0 || out != "" {
		t.Fatalf("applied: exit %d\n%s%s", code, out, errOut)
	}
	writeFile(t, file, doc("Workload", "chat", "{replicas: 3}"))
	out, _, code = run(t, "diff", "-f", file)
	if code != 1 || !strings.Contains(out, "-  replicas: 2\n+  replicas: 3") {
		t.Fatalf("changed: exit %d\n%s", code, out)
	}
	writeFile(t, file, doc("Workload", "bad", "{}"))
	if _, _, code := run(t, "diff", "-f", file); code <= 1 {
		t.Fatalf("error: exit %d, want >1", code)
	}
}

func TestApplyIfMatchSendsTheHeaderForOneDocumentOnly(t *testing.T) {
	var got string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Get("If-Match")
		_, _ = w.Write([]byte(`{"kind":"Workload"}`))
	}))
	t.Cleanup(srv.Close)
	t.Setenv("WINGSMITH_FAKE", "0")
	t.Setenv("WINGSMITH_SERVER", srv.URL)
	t.Setenv("WINGSMITH_TOKEN", "wss1_test")
	t.Setenv("WINGSMITH_PROJECT", "core")
	file := filepath.Join(t.TempDir(), "m.yaml")
	writeFile(t, file, doc("Workload", "chat", "{}"))
	if _, errOut, code := run(t, "apply", "-f", file, "--if-match", "7"); code != 0 || got != "7" {
		t.Fatalf("exit %d, If-Match %q: %s", code, got, errOut)
	}
	writeFile(t, file, doc("Workload", "a", "{}")+"---\n"+doc("Workload", "b", "{}"))
	if _, _, code := run(t, "apply", "-f", file, "--if-match", "7"); code != 2 {
		t.Fatalf("two documents: exit %d, want 2", code)
	}
}

func TestDiffExitsAboveOneWhenThePlaneFails(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`not json`))
	}))
	t.Cleanup(srv.Close)
	garbled := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`not json`))
	}))
	t.Cleanup(garbled.Close)
	down := httptest.NewServer(http.NotFoundHandler())
	down.Close()
	file := filepath.Join(t.TempDir(), "m.yaml")
	writeFile(t, file, doc("Workload", "chat", "{}"))
	t.Setenv("WINGSMITH_FAKE", "0")
	t.Setenv("WINGSMITH_TOKEN", "wss1_test")
	t.Setenv("WINGSMITH_PROJECT", "core")
	for name, url := range map[string]string{"500": srv.URL, "unreachable": down.URL, "unparsable": garbled.URL} {
		t.Setenv("WINGSMITH_SERVER", url)
		if _, errOut, code := run(t, "diff", "-f", file); code <= 1 || errOut == "" {
			t.Errorf("%s: exit %d, want >1, stderr %q", name, code, errOut)
		}
	}
}

func TestApplyFieldManagerAndForceConflictsReachTheQuery(t *testing.T) {
	var got []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = append(got, r.URL.RawQuery)
		_, _ = w.Write([]byte(`{"kind":"Workload"}`))
	}))
	t.Cleanup(srv.Close)
	t.Setenv("WINGSMITH_FAKE", "0")
	t.Setenv("WINGSMITH_SERVER", srv.URL)
	t.Setenv("WINGSMITH_TOKEN", "wss1_test")
	t.Setenv("WINGSMITH_PROJECT", "core")
	file := filepath.Join(t.TempDir(), "m.yaml")
	writeFile(t, file, doc("Workload", "chat", "{}"))
	if _, errOut, code := run(t, "apply", "-f", file); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	if _, errOut, code := run(t, "apply", "-f", file, "--field-manager", "argo", "--force-conflicts"); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	want := []string{"", "fieldManager=argo&force=true"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("queries %q, want %q", got, want)
	}
}
