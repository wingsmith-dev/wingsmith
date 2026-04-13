package health

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// serve starts a health server on a free port and returns its base URL. The listener is opened
// first so the test never races the goroutine for the port.
func serve(t *testing.T, checks map[string]Check) (string, context.CancelFunc, <-chan error) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve a port: %v", err)
	}
	addr := ln.Addr().String()
	if err := ln.Close(); err != nil {
		t.Fatalf("release the reserved port: %v", err)
	}

	s := New(addr, quiet(), checks)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	// stopped is closed rather than sent on, so cleanup can wait for the goroutine even when the
	// test has already taken the error off done.
	stopped := make(chan struct{})
	go func() { done <- s.Run(ctx); close(stopped) }()

	base := "http://" + addr
	deadline := time.Now().Add(3 * time.Second)
	for {
		resp, err := http.Get(base + "/healthz")
		if err == nil {
			_ = resp.Body.Close()
			break
		}
		if time.Now().After(deadline) {
			cancel()
			<-stopped
			t.Fatalf("health server never came up on %s: %v", addr, err)
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Cleanup(func() { cancel(); <-stopped })
	return base, cancel, done
}

func get(t *testing.T, url string) (int, string) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("read %s: %v", url, err)
	}
	return resp.StatusCode, string(body)
}

// TestLivenessIsUnconditional is the architectural claim in the package doc, held as a test: a
// partitioned operator is still alive, because the cluster keeps working when the plane is not
// reachable. If liveness ever started consulting the checks, a control-plane outage would restart
// every operator in the fleet.
func TestLivenessIsUnconditional(t *testing.T) {
	base, _, _ := serve(t, map[string]Check{
		"sync": func() error { return errors.New("stream is down") },
	})
	if code, body := get(t, base+"/healthz"); code != http.StatusOK || !strings.Contains(body, "ok") {
		t.Fatalf("liveness answered %d %q while a check was failing", code, body)
	}
}

func TestReadinessIsOKWhenEveryCheckPasses(t *testing.T) {
	base, _, _ := serve(t, map[string]Check{
		"sync":  func() error { return nil },
		"cache": func() error { return nil },
	})
	if code, body := get(t, base+"/readyz"); code != http.StatusOK || !strings.Contains(body, "ok") {
		t.Fatalf("readiness answered %d %q with every check passing", code, body)
	}
}

func TestReadinessWithNoChecksIsOK(t *testing.T) {
	base, _, _ := serve(t, nil)
	if code, _ := get(t, base+"/readyz"); code != http.StatusOK {
		t.Fatalf("readiness answered %d with no checks registered", code)
	}
}

// TestReadinessNamesTheCheckThatFailed: the body is what an operator reads off `kubectl describe`,
// so a bare 503 would send them to the logs for something the response could have told them.
func TestReadinessNamesTheCheckThatFailed(t *testing.T) {
	base, _, _ := serve(t, map[string]Check{
		"sync": func() error { return errors.New("no session since 12:04") },
	})
	code, body := get(t, base+"/readyz")
	if code != http.StatusServiceUnavailable {
		t.Fatalf("readiness answered %d with a failing check, want 503", code)
	}
	if !strings.Contains(body, "sync") || !strings.Contains(body, "no session since 12:04") {
		t.Errorf("readiness body does not name the check and its reason: %q", body)
	}
}

// TestReadinessFollowsTheCheckBackToReady proves the check is consulted per request rather than
// sampled once at construction.
func TestReadinessFollowsTheCheckBackToReady(t *testing.T) {
	failing := true
	base, _, _ := serve(t, map[string]Check{
		"sync": func() error {
			if failing {
				return errors.New("connecting")
			}
			return nil
		},
	})
	if code, _ := get(t, base+"/readyz"); code != http.StatusServiceUnavailable {
		t.Fatalf("readiness answered %d while the check was failing", code)
	}
	failing = false
	if code, _ := get(t, base+"/readyz"); code != http.StatusOK {
		t.Fatalf("readiness answered %d after the check recovered", code)
	}
}

// TestOnlyGETIsRouted: the handlers are registered with a method pattern, so a POST is a 405 and
// not a silent success. A probe misconfigured as POST should fail loudly.
func TestOnlyGETIsRouted(t *testing.T) {
	base, _, _ := serve(t, nil)
	for _, path := range []string{"/healthz", "/readyz"} {
		resp, err := http.Post(base+path, "text/plain", strings.NewReader(""))
		if err != nil {
			t.Fatalf("POST %s: %v", path, err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode == http.StatusOK {
			t.Errorf("POST %s answered 200; the method pattern is not being enforced", path)
		}
	}
}

func TestUnknownPathIs404(t *testing.T) {
	base, _, _ := serve(t, nil)
	if code, _ := get(t, base+"/metrics"); code != http.StatusNotFound {
		t.Fatalf("an unrouted path answered %d, want 404", code)
	}
}

// TestRunShutsDownOnContextCancel: Run must return nil on a graceful stop, not the
// http.ErrServerClosed that ListenAndServe hands back, or every clean shutdown logs an error.
func TestRunShutsDownOnContextCancel(t *testing.T) {
	base, cancel, done := serve(t, nil)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("graceful shutdown returned %v, want nil", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Run did not return within 10s of cancellation")
	}
	if _, err := http.Get(base + "/healthz"); err == nil {
		t.Error("the listener is still accepting after shutdown")
	}
}

// TestRunReportsAListenFailure: a port already taken must surface as an error from Run rather than
// a process that looks up but is serving nothing.
func TestRunReportsAListenFailure(t *testing.T) {
	held, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("hold a port: %v", err)
	}
	defer func() { _ = held.Close() }()

	s := New(held.Addr().String(), quiet(), nil)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := s.Run(ctx); err == nil {
		t.Fatal("Run returned nil binding a port that was already held")
	}
}
