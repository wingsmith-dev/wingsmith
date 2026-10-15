package dynamo

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
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

			handler := NewHandler(tc.target(t, upstream), 0)
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

	proxy := httptest.NewServer(NewHandler(targetFor(t, upstream), 0))
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

	proxy := httptest.NewServer(NewHandler(targetFor(t, upstream), 0))
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
	handler := NewHandler(target, 0)
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

// theResolverAnswersPerDeployment is a compile-time assertion and not
// decoration. main.go hands the handler a *Resolver as a Target, so a Resolver
// that stopped satisfying DeploymentTarget would still build, still start, and
// still serve — every request quietly falling back to the shared frontend, with
// nothing in a log saying the per-model routing had stopped happening.
var theResolverAnswersPerDeployment DeploymentTarget = (*Resolver)(nil)

// setTarget is a Target that answers per deployment, which is what a cluster
// rendering a frontend per model looks like from the handler.
type setTarget struct {
	shared       *url.URL
	byDeployment map[string]*url.URL
}

func (s setTarget) URL() (*url.URL, error) {
	if s.shared == nil {
		return nil, ErrNoFrontend
	}
	return s.shared, nil
}

func (s setTarget) URLFor(deploymentID string) (*url.URL, error) {
	if address, ok := s.byDeployment[deploymentID]; ok {
		return address, nil
	}
	if s.shared != nil {
		return s.shared, nil
	}
	return nil, ErrNoDeploymentFrontend
}

// TestTheHeaderPicksTheFrontendAndNeverReachesIt is the routing decision and the
// cleanup of it, together, because they are one contract: the agent acts on the
// header and then it is spent. Dynamo has never heard of a deployment and a
// header naming one is noise on its side of the hop.
func TestTheHeaderPicksTheFrontendAndNeverReachesIt(t *testing.T) {
	shared, sharedSeen := namedFrontend(t, "shared")
	chat, chatSeen := namedFrontend(t, "chat")

	handler := NewHandler(setTarget{
		shared:       targetFor(t, shared).url,
		byDeployment: map[string]*url.URL{"dep_01j9x2": targetFor(t, chat).url},
	}, 0)

	for _, tc := range []struct {
		name       string
		deployment string
		want       string
		seen       *http.Header
	}{
		{
			name:       "the deployment's own frontend",
			deployment: "dep_01j9x2",
			want:       "chat",
			seen:       chatSeen,
		},
		{
			name:       "a deployment this cluster renders no frontend for falls back",
			deployment: "dep_never_heard_of",
			want:       "shared",
			seen:       sharedSeen,
		},
		{
			name: "no header at all falls back",
			want: "shared",
			seen: sharedSeen,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
				strings.NewReader(`{"model":"qwen3"}`))
			if tc.deployment != "" {
				request.Header.Set(tunnel.HeaderDeploymentID, tc.deployment)
			}
			recorder := httptest.NewRecorder()
			handler.ServeHTTP(recorder, request)

			if recorder.Code != http.StatusOK {
				t.Fatalf("status = %d, body %s", recorder.Code, recorder.Body)
			}
			if recorder.Body.String() != tc.want {
				t.Fatalf("body = %q, want the %s frontend", recorder.Body, tc.want)
			}
			if got := tc.seen.Get(tunnel.HeaderDeploymentID); got != "" {
				t.Fatalf("the frontend saw %s = %q, want it stripped at this hop",
					tunnel.HeaderDeploymentID, got)
			}
		})
	}
}

// TestASingleFrontendClusterIgnoresTheHeaderEntirely is what lets this ship
// dark. Every cluster runs one shared frontend and none renders a per-deployment
// one yet, so a gateway that has started sending the header must change nothing
// at all: the same address, the same body, and a frontend that cannot tell the
// two requests apart.
func TestASingleFrontendClusterIgnoresTheHeaderEntirely(t *testing.T) {
	var seen []string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		seen = append(seen, r.Method+" "+r.URL.Path+" "+string(body)+" "+
			r.Header.Get(tunnel.HeaderDeploymentID))
		_, _ = w.Write([]byte("ok"))
	}))
	defer upstream.Close()

	handler := NewHandler(targetFor(t, upstream), 0)
	for _, deployment := range []string{"", "dep_01j9x2"} {
		request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
			strings.NewReader(`{"model":"qwen3"}`))
		if deployment != "" {
			request.Header.Set(tunnel.HeaderDeploymentID, deployment)
		}
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, request)
		if recorder.Code != http.StatusOK || recorder.Body.String() != "ok" {
			t.Fatalf("status = %d, body %s", recorder.Code, recorder.Body)
		}
	}

	if len(seen) != 2 || seen[0] != seen[1] {
		t.Fatalf("the frontend saw %q, want two requests it cannot tell apart", seen)
	}
	if !strings.HasSuffix(seen[0], " ") {
		t.Fatalf("the frontend saw %q, want no deployment header on it", seen[0])
	}
}

// TestANamedDeploymentWithNoFrontendAndNoFallbackIsItsOwn503 pins the second
// refusal. Both are 503 and both are worth retrying; the code is what tells a
// caller — and whoever reads the log — that one model is mid-rollout rather than
// that the cluster is serving nothing.
func TestANamedDeploymentWithNoFrontendAndNoFallbackIsItsOwn503(t *testing.T) {
	handler := NewHandler(setTarget{byDeployment: map[string]*url.URL{}}, 0)

	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"qwen3"}`))
	request.Header.Set(tunnel.HeaderDeploymentID, "dep_still_starting")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", recorder.Code)
	}
	var envelope errorEnvelope
	if err := json.Unmarshal(recorder.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if envelope.Error.Code != "deployment_frontend_not_ready" {
		t.Fatalf("code = %q, want it distinguishable from no_frontend", envelope.Error.Code)
	}

	// The same handler, the same absence, and a request that names no
	// deployment: this is the cluster-serves-nothing answer and it must not have
	// been replaced by the one above.
	recorder = httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"qwen3"}`)))
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", recorder.Code)
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if envelope.Error.Code != "no_frontend" {
		t.Fatalf("code = %q, want the cluster-wide refusal", envelope.Error.Code)
	}
}

// namedFrontend is a frontend that answers with its own name and records the
// headers it was sent, so a test can say which one took a request.
func namedFrontend(t *testing.T, name string) (*httptest.Server, *http.Header) {
	t.Helper()
	var seen http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
		_, _ = w.Write([]byte(name))
	}))
	t.Cleanup(server.Close)
	return server, &seen
}

// The agent is the innermost of three hops arming a response-header timer, and
// only the smallest of the three is ever observed. A caller that passes nothing
// must therefore land above the hops outside it, not on a bare zero — which
// net/http reads as no bound at all — and not on some small local constant.
func TestAnUnsetBoundLandsAboveTheHopsOutsideThisOne(t *testing.T) {
	for _, tc := range []struct {
		name      string
		configure time.Duration
		want      time.Duration
	}{
		{"unset", 0, tunnel.InnerHopTimeout(0)},
		{"negative is not no bound", -time.Second, tunnel.InnerHopTimeout(0)},
		{"derived from a raised budget", tunnel.InnerHopTimeout(90 * time.Minute), 91 * time.Minute},
	} {
		t.Run(tc.name, func(t *testing.T) {
			transport, ok := newReverseProxy(tc.configure).Transport.(*http.Transport)
			if !ok {
				t.Fatal("the frontend proxy is not on a transport this hop configures")
			}
			if transport.ResponseHeaderTimeout != tc.want {
				t.Fatalf("response header timeout = %s, want %s", transport.ResponseHeaderTimeout, tc.want)
			}
			if transport.ResponseHeaderTimeout <= tunnel.DefaultResponseHeaderTimeout {
				t.Fatalf("this hop gives up at %s, before the gateway's %s, so the gateway's bound means nothing",
					transport.ResponseHeaderTimeout, tunnel.DefaultResponseHeaderTimeout)
			}
		})
	}
}

// The bound still exists. A frontend that accepted a request and has said
// nothing is wedged rather than thinking, and the point of making the number
// configurable was to size it correctly, not to delete it.
func TestAFrontendThatSaysNothingIsStillGivenUpOn(t *testing.T) {
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
	}))
	defer upstream.Close()
	defer close(release)

	handler := NewHandler(targetFor(t, upstream), 200*time.Millisecond)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"qwen3"}`))

	started := time.Now()
	handler.ServeHTTP(recorder, request)
	elapsed := time.Since(started)

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 (body %s)", recorder.Code, recorder.Body)
	}
	if elapsed < 200*time.Millisecond {
		t.Errorf("gave up after %s, before the bound it was given", elapsed)
	}
	if elapsed > 10*time.Second {
		t.Errorf("took %s to give up on a 200ms bound", elapsed)
	}
}

// A failure in the cluster is the server's, and its error type has to say so: a client that
// branches on invalid_request_error does not retry, and a frontend outage is worth retrying.
func TestAServerFailureIsTypedAsOne(t *testing.T) {
	for _, tc := range []struct {
		target Target
		status int
	}{
		{fixedTarget{err: ErrNoFrontend}, http.StatusServiceUnavailable},
		{fixedTarget{err: errors.New("broken")}, http.StatusInternalServerError},
	} {
		recorder := httptest.NewRecorder()
		NewHandler(tc.target, 0).ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil))
		if recorder.Code != tc.status {
			t.Fatalf("status = %d, want %d", recorder.Code, tc.status)
		}
		var envelope errorEnvelope
		if err := json.Unmarshal(recorder.Body.Bytes(), &envelope); err != nil {
			t.Fatal(err)
		}
		if envelope.Error.Type != "server_error" {
			t.Errorf("a %d is typed %q, want server_error", tc.status, envelope.Error.Type)
		}
	}
}

// The gateway puts the caller's address in X-Forwarded-For. On a tunnel the peer address is the
// gateway's, so replacing the header with it would lose the caller.
func TestForwardedForIsAppendedTo(t *testing.T) {
	seen := make(chan string, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen <- r.Header.Get("X-Forwarded-For")
	}))
	defer upstream.Close()

	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{}`))
	request.Header.Set("X-Forwarded-For", "203.0.113.7")
	NewHandler(targetFor(t, upstream), 0).ServeHTTP(httptest.NewRecorder(), request)

	got := <-seen
	if !strings.HasPrefix(got, "203.0.113.7, ") {
		t.Errorf("X-Forwarded-For = %q, want the caller's address kept first", got)
	}
}
