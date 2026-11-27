package serve

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// fakeDocker is an Engine API daemon over a unix socket, holding containers in memory. Every
// started container is "published" on the port of probeSrv, so readiness probes hit a real
// listener.
type fakeDocker struct {
	mu         sync.Mutex
	containers map[string]*fakeContainer
	images     map[string]bool
	pullErr    string
	seq        int
	hostPort   string
	creates    []ContainerCreate
}

type fakeContainer struct {
	id, name string
	spec     ContainerCreate
	running  bool
	restarts int
	exitCode int
}

func newFakeDocker(t *testing.T, hostPort string) (*fakeDocker, *DockerClient) {
	t.Helper()
	f := &fakeDocker{containers: map[string]*fakeContainer{}, images: map[string]bool{}, hostPort: hostPort}
	dir, err := os.MkdirTemp("", "dk")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	sock := filepath.Join(dir, "d.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(f.serve))
	srv.Listener = ln
	srv.Start()
	t.Cleanup(srv.Close)
	return f, NewDockerClient(sock)
}

func (f *fakeDocker) byRef(ref string) *fakeContainer {
	if c, ok := f.containers[ref]; ok {
		return c
	}
	for _, c := range f.containers {
		if c.name == ref {
			return c
		}
	}
	return nil
}

func (f *fakeDocker) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	path := strings.TrimPrefix(r.URL.Path, dockerAPI)
	notFound := func(msg string) {
		w.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(w).Encode(map[string]string{"message": msg})
	}
	switch {
	case r.Method == http.MethodGet && path == "/containers/json":
		var filters map[string][]string
		_ = json.Unmarshal([]byte(r.URL.Query().Get("filters")), &filters)
		out := []ContainerSummary{}
		for _, c := range f.containers {
			match := true
			for _, kv := range filters["label"] {
				k, v, _ := strings.Cut(kv, "=")
				if c.spec.Labels[k] != v {
					match = false
				}
			}
			if match {
				out = append(out, ContainerSummary{ID: c.id, Names: []string{"/" + c.name}, Image: c.spec.Image, Labels: c.spec.Labels})
			}
		}
		_ = json.NewEncoder(w).Encode(out)
	case r.Method == http.MethodPost && path == "/containers/create":
		var spec ContainerCreate
		_ = json.NewDecoder(r.Body).Decode(&spec)
		if !f.images[spec.Image] {
			notFound("No such image: " + spec.Image)
			return
		}
		name := r.URL.Query().Get("name")
		if f.byRef(name) != nil {
			w.WriteHeader(http.StatusConflict)
			return
		}
		f.seq++
		c := &fakeContainer{id: fmt.Sprintf("c%d", f.seq), name: name, spec: spec}
		f.containers[c.id] = c
		f.creates = append(f.creates, spec)
		_ = json.NewEncoder(w).Encode(map[string]string{"Id": c.id})
	case r.Method == http.MethodPost && path == "/images/create":
		if f.pullErr != "" {
			_, _ = fmt.Fprintf(w, `{"status":"pulling"}`+"\n"+`{"error":%q}`+"\n", f.pullErr)
			return
		}
		f.images[r.URL.Query().Get("fromImage")] = true
		_, _ = w.Write([]byte(`{"status":"done"}`))
	case strings.HasPrefix(path, "/containers/"):
		rest := strings.TrimPrefix(path, "/containers/")
		ref, action, _ := strings.Cut(rest, "/")
		c := f.byRef(ref)
		if c == nil {
			notFound("No such container: " + ref)
			return
		}
		switch {
		case r.Method == http.MethodPost && action == "start":
			c.running = true
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet && action == "json":
			var info ContainerInspect
			info.ID, info.Name, info.RestartCount = c.id, "/"+c.name, c.restarts
			info.State.Running = c.running
			info.State.ExitCode = c.exitCode
			info.State.Status = map[bool]string{true: "running", false: "exited"}[c.running]
			info.Config.Labels = c.spec.Labels
			if c.running {
				info.NetworkSettings.Ports = map[string][]PortBinding{}
				for p := range c.spec.HostConfig.PortBindings {
					info.NetworkSettings.Ports[p] = []PortBinding{{HostIP: "127.0.0.1", HostPort: f.hostPort}}
				}
			}
			_ = json.NewEncoder(w).Encode(info)
		case r.Method == http.MethodDelete && action == "":
			delete(f.containers, c.id)
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusNotImplemented)
		}
	default:
		w.WriteHeader(http.StatusNotImplemented)
	}
}
