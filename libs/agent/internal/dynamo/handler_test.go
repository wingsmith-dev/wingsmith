package dynamo

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// fixedTarget is a Target that answers with whatever it was given, so the
// handler's own behaviour can be tested without a cluster in the way.
type fixedTarget struct {
	url *url.URL
	err error
}

func (f fixedTarget) URL() (*url.URL, error) { return f.url, f.err }

func targetFor(t *testing.T, server *httptest.Server) fixedTarget {
	t.Helper()
	parsed, err := url.Parse(server.URL)
	if err != nil {
		t.Fatalf("parse %q: %v", server.URL, err)
	}
	return fixedTarget{url: parsed}
}

func TestForwarding(t *testing.T) {
	cases := []struct {
		name       string
		target     func(t *testing.T, upstream *httptest.Server) Target
		wantStatus int
		wantBody   string
		wantPath   string
	}{
		{
			name: "a request reaches the frontend unchanged",
			target: func(t *testing.T, upstream *httptest.Server) Target {
				return targetFor(t, upstream)
			},
			wantStatus: http.StatusOK,
			wantBody:   `{"ok":true}`,
			wantPath:   "/v1/chat/completions",
		},
		{
			name: "no frontend yet is unavailable, not a bad gateway",
			target: func(*testing.T, *httptest.Server) Target {
				return fixedTarget{err: ErrNoFrontend}
			},
			wantStatus: http.StatusServiceUnavailable,
			wantBody:   `"code":"no_frontend"`,
		},
		{
			name: "any other resolution failure is ours",
			target: func(*testing.T, *httptest.Server) Target {
				return fixedTarget{err: errors.New("broken")}
			},
			wantStatus: http.StatusInternalServerError,
			wantBody:   `"code":"frontend_error"`,
		},
		{
			name: "a frontend that is not listening is a bad gateway",
			target: func(t *testing.T, upstream *httptest.Server) Target {
				target := targetFor(t, upstream)
				upstream.Close()
				return target
			},
			wantStatus: http.StatusBadGateway,
			wantBody:   `"code":"frontend_unavailable"`,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var seenPath, seenMethod string
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				seenPath, seenMethod = r.URL.Path, r.Method
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"ok":true}`))
			}))
			defer upstream.Close()

			handler := NewHandler(tc.target(t, upstream))
			recorder := httptest.NewRecorder()
			request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
				strings.NewReader(`{"model":"qwen3"}`))
			handler.ServeHTTP(recorder, request)

			if recorder.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d (body %s)", recorder.Code, tc.wantStatus, recorder.Body)
			}
			if !strings.Contains(recorder.Body.String(), tc.wantBody) {
				t.Fatalf("body = %s, want it to contain %s", recorder.Body, tc.wantBody)
			}
			if tc.wantPath != "" {
				if seenPath != tc.wantPath {
					t.Fatalf("frontend saw path %q, want %q", seenPath, tc.wantPath)
				}
				if seenMethod != http.MethodPost {
					t.Fatalf("frontend saw method %q, want POST", seenMethod)
				}
			}
		})
	}
}

// TestAStreamArrivesTokenByToken is what FlushInterval: -1 buys. Without it the
// proxy buffers a body with no known length, and a caller that asked for a
// stream gets one long pause followed by the whole answer.
func TestAStreamArrivesTokenByToken(t *testing.T) {
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		controller := http.NewResponseController(w)
		for i := range 3 {
			_, _ = fmt.Fprintf(w, "data: chunk-%d\n\n", i)
			if err := controller.Flush(); err != nil {
				t.Errorf("flush: %v", err)
				return
			}
			select {
			case <-release:
			case <-r.Context().Done():
				return
			case <-time.After(5 * time.Second):
				t.Error("the reader never asked for the next chunk")
				return
			}
		}
	}))
	defer upstream.Close()

	proxy := httptest.NewServer(NewHandler(targetFor(t, upstream)))
	defer proxy.Close()

	response, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json",
		strings.NewReader(`{"model":"qwen3","stream":true}`))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer response.Body.Close()

	reader := bufio.NewReader(response.Body)
	for i := range 3 {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatalf("read chunk %d: %v", i, err)
		}
		if want := fmt.Sprintf("data: chunk-%d\n", i); line != want {
			t.Fatalf("chunk %d = %q, want %q", i, line, want)
		}
		if _, err := reader.ReadString('\n'); err != nil {
			t.Fatalf("read blank line after chunk %d: %v", i, err)
		}
		release <- struct{}{}
	}
}

// TestAClientHangingUpAbortsTheFrontendCall matters because a completion holds a
// GPU. A cancelled request whose upstream keeps generating is capacity spent on
// an answer nobody will read.
func TestAClientHangingUpAbortsTheFrontendCall(t *testing.T) {
	started := make(chan struct{})
	aborted := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write([]byte("data: first\n\n"))
		_ = http.NewResponseController(w).Flush()
		close(started)
		select {
		case <-r.Context().Done():
			close(aborted)
		case <-time.After(5 * time.Second):
			t.Error("the frontend was never told the caller had gone")
		}
	}))
	defer upstream.Close()

	proxy := httptest.NewServer(NewHandler(targetFor(t, upstream)))
	defer proxy.Close()

	ctx, cancel := context.WithCancel(context.Background())
	request, err := http.NewRequestWithContext(ctx, http.MethodPost,
		proxy.URL+"/v1/chat/completions", strings.NewReader(`{"model":"qwen3","stream":true}`))
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer response.Body.Close()

	buffer := make([]byte, len("data: first\n\n"))
	if _, err := io.ReadFull(response.Body, buffer); err != nil {
		t.Fatalf("read the first chunk: %v", err)
	}
	<-started
	cancel()

	select {
	case <-aborted:
	case <-time.After(5 * time.Second):
		t.Fatal("cancellation did not reach the frontend")
	}
}

// movingTarget is a Target whose address changes underneath the handler, which
// is what a redeployed graph looks like from here.
type movingTarget struct{ url *url.URL }

func (m *movingTarget) URL() (*url.URL, error) { return m.url, nil }

// TestTheHandlerReadsTheAddressPerRequest is the other half of a frontend that
// moves: discovery finding the new Service is worthless if the handler bound the
// old address into its proxy at construction.
func TestTheHandlerReadsTheAddressPerRequest(t *testing.T) {
	first := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("first"))
	}))
	defer first.Close()
	second := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("second"))
	}))
	defer second.Close()

	target := &movingTarget{url: targetFor(t, first).url}
	handler := NewHandler(target)
	if body := call(t, handler); body != "first" {
		t.Fatalf("body = %q, want the first frontend", body)
	}

	target.url = targetFor(t, second).url
	if body := call(t, handler); body != "second" {
		t.Fatalf("body = %q, want the frontend it moved to", body)
	}
}

func call(t *testing.T, handler http.Handler) string {
	t.Helper()
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"qwen3"}`)))
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, body %s", recorder.Code, recorder.Body)
	}
	return recorder.Body.String()
}
