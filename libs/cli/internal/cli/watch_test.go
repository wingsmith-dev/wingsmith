package cli

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func obj(name, rv string) string {
	return fmt.Sprintf(`{"apiVersion":"plane.wingsmith.dev/v1alpha1","kind":"Workload","metadata":{"name":%q,"namespace":"core","resourceVersion":%q}}`, name, rv)
}

func TestGetWatchStreamsEventsAndRelistsOnAnExpiredResourceVersion(t *testing.T) {
	var mu sync.Mutex
	var seen []string
	lists := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		q := r.URL.Query()
		seen = append(seen, r.URL.Path+" watch="+q.Get("watch")+" rv="+q.Get("resourceVersion"))
		w.Header().Set("Content-Type", "application/json")
		if q.Get("watch") != "true" {
			lists++
			rv := map[int]string{1: "5", 2: "7"}[lists]
			fmt.Fprintf(w, `{"kind":"WorkloadList","metadata":{"resourceVersion":%q},"items":[%s]}`, rv, obj("chat", rv))
			return
		}
		switch q.Get("resourceVersion") {
		case "5":
			fmt.Fprintf(w, "{\"type\":\"MODIFIED\",\"object\":%s}\n", obj("chat", "6"))
			fmt.Fprint(w, `{"type":"ERROR","object":{"kind":"Status","code":410,"reason":"Expired","message":"gone"}}`+"\n")
		case "7":
			fmt.Fprint(w, `{"type":"BOOKMARK","object":{"kind":"Workload","metadata":{"resourceVersion":"8"}}}`+"\n")
			fmt.Fprintf(w, "{\"type\":\"DELETED\",\"object\":%s}\n", obj("chat", "9"))
		default:
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprint(w, `{"code":"unavailable","message":"stop here"}`)
		}
	}))
	t.Cleanup(srv.Close)
	t.Setenv("WINGSMITH_FAKE", "0")
	t.Setenv("WINGSMITH_SERVER", srv.URL)
	t.Setenv("WINGSMITH_TOKEN", "wss1_test")
	t.Setenv("WINGSMITH_PROJECT", "core")

	out, errOut, code := run(t, "get", "workloads", "-w", "-o", "human")
	if code == 0 || !strings.Contains(errOut, "stop here") {
		t.Fatalf("want the final refusal to end the watch, exit %d: %s", code, errOut)
	}
	want := []string{
		"ADDED\tworkload/chat\tcore\t5",
		"MODIFIED\tworkload/chat\tcore\t6",
		"ADDED\tworkload/chat\tcore\t7",
		"DELETED\tworkload/chat\tcore\t9",
	}
	if got := strings.Split(strings.TrimSpace(out), "\n"); strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Errorf("events:\n%s\nwant:\n%s", out, strings.Join(want, "\n"))
	}
	wantReqs := []string{
		"/v1/manifests/workloads/core watch= rv=",
		"/v1/manifests/workloads/core watch=true rv=5",
		"/v1/manifests/workloads/core watch= rv=",
		"/v1/manifests/workloads/core watch=true rv=7",
		"/v1/manifests/workloads/core watch=true rv=9",
	}
	if strings.Join(seen, "\n") != strings.Join(wantReqs, "\n") {
		t.Errorf("requests:\n%s", strings.Join(seen, "\n"))
	}
}

func TestGetWithoutANameListsTheKind(t *testing.T) {
	t.Setenv("WINGSMITH_PROJECT", "core")
	out, errOut, code := run(t, "get", "workloads", "-o", "json")
	if code != 0 || !strings.Contains(out, `"items"`) {
		t.Fatalf("exit %d: %s %s", code, out, errOut)
	}
}

func TestGetWatchBacksOffWhenThePlaneClosesStreamsAtOnce(t *testing.T) {
	watches := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Query().Get("watch") != "true" {
			fmt.Fprintf(w, `{"kind":"WorkloadList","metadata":{"resourceVersion":"5"},"items":[]}`)
			return
		}
		watches++
		if watches == 3 {
			fmt.Fprintf(w, "{\"type\":\"MODIFIED\",\"object\":%s}\n", obj("chat", "6"))
		}
	}))
	t.Cleanup(srv.Close)
	t.Setenv("WINGSMITH_FAKE", "0")
	t.Setenv("WINGSMITH_SERVER", srv.URL)
	t.Setenv("WINGSMITH_TOKEN", "wss1_test")
	t.Setenv("WINGSMITH_PROJECT", "core")

	stop := errors.New("stop")
	var delays []time.Duration
	orig := watchSleep
	watchSleep = func(_ context.Context, d time.Duration) error {
		delays = append(delays, d)
		if len(delays) == 8 {
			return stop
		}
		return nil
	}
	t.Cleanup(func() { watchSleep = orig })

	_, errOut, code := run(t, "get", "workloads", "-w", "-o", "human")
	if code == 0 {
		t.Fatalf("want the stub's error to end the watch: %s", errOut)
	}
	want := []time.Duration{1, 2, 1, 2, 4, 8, 16, 30}
	for i := range want {
		want[i] *= time.Second
	}
	if fmt.Sprint(delays) != fmt.Sprint(want) {
		t.Errorf("delays %v, want %v (reset after the event on the third watch)", delays, want)
	}
}
