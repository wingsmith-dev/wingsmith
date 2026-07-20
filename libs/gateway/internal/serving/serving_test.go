package serving

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

type stubResolver struct {
	routes map[string]routing.Route
}

func (s stubResolver) Resolve(name string) (routing.Route, error) {
	route, ok := s.routes[name]
	if !ok {
		return routing.Route{}, routing.ErrUnknownEndpoint
	}
	return route, nil
}

func (s stubResolver) Names() []string {
	names := make([]string, 0, len(s.routes))
	for name := range s.routes {
		names = append(names, name)
	}
	return names
}

func (s stubResolver) Revision() string { return "1" }

func upstreamServer(t *testing.T) (*httptest.Server, *http.Request) {
	t.Helper()
	var seen *http.Request
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		r.Body = io.NopCloser(strings.NewReader(string(body)))
		seen = r
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	t.Cleanup(server.Close)
	return server, seen
}

func routeTo(t *testing.T, address string, headers http.Header) routing.Route {
	t.Helper()
	parsed, err := url.Parse(address)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return routing.NewRoute("chat", "weighted",
		routing.NewUpstream("ap", 100, true, NewReverseProxy(parsed, headers, 0)))
}

func TestBodyIsForwardedVerbatim(t *testing.T) {
	server, _ := upstreamServer(t)
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, nil),
	}}})

	body := `{"model":"chat","messages":[{"role":"user","content":"hi"}]}`
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body)))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	if rec.Body.String() != body {
		t.Fatalf("body = %s, want it forwarded untouched", rec.Body.String())
	}
}

func TestUnknownModelIsNotFoundAndEmptyRouteIsCapacity(t *testing.T) {
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routing.NewRoute("chat", "weighted"),
	}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"nope"}`)))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown model = %d, want 404", rec.Code)
	}

	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"chat"}`)))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("no upstream = %d, want 503", rec.Code)
	}
}

func TestMalformedRequestsAreRejectedInTheOpenAIShape(t *testing.T) {
	handler := New(Config{Router: stubResolver{}})

	for _, tc := range []struct {
		name string
		body string
		code string
	}{
		{"not json", `{`, "invalid_json"},
		{"no model", `{"messages":[]}`, "missing_model"},
	} {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(tc.body)))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s: status = %d", tc.name, rec.Code)
		}
		var envelope errorEnvelope
		if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if envelope.Error.Code != tc.code {
			t.Fatalf("%s: code = %q", tc.name, envelope.Error.Code)
		}
	}
}

func TestAtMostFourStopSequencesReachTheEngine(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, nil),
	}}})

	for _, tc := range []struct {
		name       string
		path       string
		stop       string
		wantStatus int
		wantCalls  int
	}{
		{"one string", "/v1/chat/completions", `"done"`, http.StatusOK, 1},
		{"four strings", "/v1/chat/completions", `["a","b","c","d"]`, http.StatusOK, 2},
		{"five strings", "/v1/chat/completions", `["a","b","c","d","e"]`, http.StatusBadRequest, 2},
		{"legacy completions", "/v1/completions", `["a","b","c","d","e"]`, http.StatusBadRequest, 2},
		{"another protocol", "/v1/responses", `["a","b","c","d","e"]`, http.StatusOK, 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body := `{"model":"chat","stop":` + tc.stop + `}`
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, tc.path, strings.NewReader(body)))
			if rec.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d (body %s)", rec.Code, tc.wantStatus, rec.Body)
			}
			if calls != tc.wantCalls {
				t.Fatalf("upstream calls = %d, want %d", calls, tc.wantCalls)
			}
		})
	}
}

// errorFrom sends one completion at an upstream that answers with the given
// status and body, and returns what the caller got.
func errorFrom(t *testing.T, status int, header http.Header, body string) *httptest.ResponseRecorder {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		for name, values := range header {
			for _, value := range values {
				w.Header().Add(name, value)
			}
		}
		if w.Header().Get("Content-Type") == "" {
			w.Header().Set("Content-Type", "application/json")
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, nil),
	}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"chat"}`)))
	return rec
}

// TestNoUpstreamErrorBodyReachesTheCaller is the whole of the passthrough rule.
//
// Every shape an engine can answer with — a bare object, the OpenAI envelope
// already built, prose, a body over any cap this hop might read under — arrives
// as one of this hop's own objects and none of the engine's own words.
func TestNoUpstreamErrorBodyReachesTheCaller(t *testing.T) {
	secret := "kimi-k3-frontend.dynamo.svc.cluster.local"
	for _, test := range []struct {
		name   string
		status int
		body   string
		want   errorBody
	}{
		{
			name:   "a bare object, which is Dynamo's shape",
			status: http.StatusInternalServerError,
			body:   `{"message":"engine crashed in ` + secret + `","type":"Internal Server Error","code":500}`,
			want:   errorBody{Message: "The model failed to answer this request.", Type: "api_error", Code: "upstream_error"},
		},
		{
			name:   "the OpenAI envelope, which used to pass untouched",
			status: http.StatusBadRequest,
			body:   `{"error":{"message":"model ` + secret + ` does not exist","type":"invalid_request_error","code":"bad_param"}}`,
			want:   errorBody{Message: "The request was not accepted by this model.", Type: "invalid_request_error", Code: "invalid_request"},
		},
		{
			name:   "prose, which is what a framework's default handler writes",
			status: http.StatusServiceUnavailable,
			body:   "no worker for " + secret,
			want:   errorBody{Message: "This model is temporarily unavailable.", Type: "overloaded_error", Code: "no_capacity"},
		},
		{
			name:   "a body larger than anything this hop buffers",
			status: http.StatusInternalServerError,
			body:   `{"message":"` + strings.Repeat(secret+" ", 8<<10) + `"}`,
			want:   errorBody{Message: "The model failed to answer this request.", Type: "api_error", Code: "upstream_error"},
		},
		{
			name:   "a status nobody anticipated, which is still one of these objects",
			status: http.StatusTeapot,
			body:   `{"message":"` + secret + `"}`,
			want:   errorBody{Message: "The request was not accepted by this model.", Type: "invalid_request_error", Code: "invalid_request"},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			rec := errorFrom(t, test.status, nil, test.body)
			if strings.Contains(rec.Body.String(), secret) {
				t.Fatalf("body = %s, the upstream's own words survived", rec.Body)
			}
			var envelope errorEnvelope
			if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
				t.Fatalf("decode %s: %v", rec.Body, err)
			}
			if envelope.Error != test.want {
				t.Fatalf("error = %#v, want %#v", envelope.Error, test.want)
			}
		})
	}
}

// TestAnUpstreamStatusIsAnsweredWithTheStatusThisHopMeansByIt covers the three
// statuses that are not forwarded as they arrived, because forwarding them would
// tell a caller something untrue about their own request.
func TestAnUpstreamStatusIsAnsweredWithTheStatusThisHopMeansByIt(t *testing.T) {
	for _, test := range []struct {
		upstream int
		want     int
		why      string
	}{
		{http.StatusUnauthorized, http.StatusBadGateway, "the credential is ours and the caller has never seen it"},
		{http.StatusForbidden, http.StatusBadGateway, "the credential is ours and the caller has never seen it"},
		{http.StatusNotFound, http.StatusBadGateway, "the path was routed here, so a 404 is a misconfigured upstream"},
		{http.StatusBadRequest, http.StatusBadRequest, "a bad request stays the caller's"},
		{http.StatusTooManyRequests, http.StatusTooManyRequests, "a throttle stays a throttle"},
		{http.StatusServiceUnavailable, http.StatusServiceUnavailable, "capacity stays capacity"},
	} {
		rec := errorFrom(t, test.upstream, nil, `{"message":"upstream"}`)
		if rec.Code != test.want {
			t.Fatalf("upstream %d became %d, want %d: %s", test.upstream, rec.Code, test.want, test.why)
		}
	}
}

// TestOnlyForwardedResponseHeadersLeaveTheGateway is the header half of the same
// boundary. Anything behind this hop may describe itself in a header; none of it
// leaves here.
func TestOnlyForwardedResponseHeadersLeaveTheGateway(t *testing.T) {
	upstream := http.Header{
		"Content-Type":                {"application/json"},
		"Cache-Control":               {"no-cache"},
		"X-Accel-Buffering":           {"no"},
		"Retry-After":                 {"3"},
		"X-Ratelimit-Remaining":       {"41"},
		"Ratelimit-Reset":             {"60"},
		"Server":                      {"uvicorn"},
		"X-Dynamo-Worker":             {"decode-0"},
		"X-Request-Id":                {"7f3c"},
		"X-Vllm-Engine":               {"v1"},
		"Www-Authenticate":            {"Bearer realm=\"dynamo\""},
		"Access-Control-Allow-Origin": {"*"},
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		for name, values := range upstream {
			w.Header()[name] = values
		}
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, nil),
	}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"chat"}`)))

	for _, name := range []string{"Content-Type", "Cache-Control", "X-Accel-Buffering",
		"Retry-After", "X-Ratelimit-Remaining", "Ratelimit-Reset"} {
		if rec.Header().Get(name) == "" {
			t.Errorf("%s was dropped, and a caller needs it", name)
		}
	}
	for _, name := range []string{"Server", "X-Dynamo-Worker", "X-Request-Id", "X-Vllm-Engine",
		"Www-Authenticate", "Access-Control-Allow-Origin"} {
		if got := rec.Header().Get(name); got != "" {
			t.Errorf("%s = %q, and it names what is behind this hop", name, got)
		}
	}
}

// TestARedirectLeavesHereAsAPath is the leak a framework opens by construction:
// FastAPI answers a trailing slash with a 307 whose Location it builds from the
// Host header, and the Host the frontend sees is the address the agent dialled.
func TestARedirectLeavesHereAsAPath(t *testing.T) {
	for _, test := range []struct {
		upstream string
		want     string
	}{
		{"http://kimi-k3-frontend.dynamo.svc.cluster.local:8000/v1/models", "/v1/models"},
		{"https://clu_ap.tunnel.hangar.internal/v1/models?limit=2", "/v1/models?limit=2"},
		{"http://10.0.3.4:8000/v1/models#top", "/v1/models#top"},
		{"/v1/models", "/v1/models"},
	} {
		rec := errorFrom(t, http.StatusTemporaryRedirect,
			http.Header{"Location": {test.upstream}}, "")
		if got := rec.Header().Get("Location"); got != test.want {
			t.Errorf("Location = %q, want %q", got, test.want)
		}
	}
}

// TestTheCatalogDoesNotNameWhoRunsIt: owned_by is the one field on the one
// response a caller can read without sending a request, and it used to name this
// project.
func TestTheCatalogDoesNotNameWhoRunsIt(t *testing.T) {
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{"chat": {}}}})
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/models", nil))

	body := strings.ToLower(rec.Body.String())
	for _, needle := range []string{"hangar", "acme", "partner", "dynamo", "vllm"} {
		if strings.Contains(body, needle) {
			t.Fatalf("the catalog says %q: %s", needle, rec.Body)
		}
	}
	var list modelList
	if err := json.Unmarshal(rec.Body.Bytes(), &list); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if list.Data[0].OwnedBy != "system" {
		t.Fatalf("owned_by = %q", list.Data[0].OwnedBy)
	}
}

func TestModelListIsAnsweredLocally(t *testing.T) {
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": {}, "code": {},
	}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/models", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	var list modelList
	if err := json.Unmarshal(rec.Body.Bytes(), &list); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(list.Data) != 2 || list.Data[0].ID != "chat" {
		t.Fatalf("data = %v, want both models sorted", list.Data)
	}
}

func TestHealthzAnswersWithoutTheGuard(t *testing.T) {
	handler := New(Config{Router: stubResolver{}, Callers: NewTokenGuard("secret")})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz = %d, want it to answer with no token", rec.Code)
	}
}

func TestGuardRejectsCallersWithoutTheToken(t *testing.T) {
	handler := New(Config{Router: stubResolver{}, Callers: NewTokenGuard("secret")})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`)))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`))
	req.Header.Set("Authorization", "Bearer secret")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code == http.StatusUnauthorized {
		t.Fatal("the right token must be accepted")
	}
}

func TestAnEmptyTokenLeavesTheGatewayOpenRatherThanAcceptingEmptyStrings(t *testing.T) {
	if NewTokenGuard("") != nil {
		t.Fatal("an unset token must not build a guard")
	}
}

func TestCredentialHeadersAreStampedOnTheOutboundLegOnly(t *testing.T) {
	var seen http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	headers := http.Header{}
	headers.Set("X-Access-Token", "from-the-mount")
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, headers),
	}}})

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`))
	req.Header.Set("X-Access-Token", "from-the-caller")
	handler.ServeHTTP(httptest.NewRecorder(), req)

	if seen.Get("X-Access-Token") != "from-the-mount" {
		t.Fatalf("upstream saw %q, want the caller's value overwritten", seen.Get("X-Access-Token"))
	}
}

func TestModelIsRewrittenToTheNameTheEngineAnswersTo(t *testing.T) {
	server, _ := upstreamServer(t)
	route := routeTo(t, server.URL, nil)
	route.Upstreams[0].ServedModelName = "llama-3-3-70b-instruct-fp8-tp2-32k"
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{"chat": route}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"chat","messages":[{"role":"user","content":"hi"}],"stream":true}`)))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var forwarded map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &forwarded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if forwarded["model"] != "llama-3-3-70b-instruct-fp8-tp2-32k" {
		t.Fatalf("model = %v, want the served name", forwarded["model"])
	}
	if forwarded["stream"] != true {
		t.Fatalf("stream = %v, want every other field carried through", forwarded["stream"])
	}
	if _, ok := forwarded["messages"].([]any); !ok {
		t.Fatalf("messages = %v, want them intact", forwarded["messages"])
	}
}

func TestModelIsUntouchedWhenThePlaneNamesNoServedModel(t *testing.T) {
	server, _ := upstreamServer(t)
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, nil),
	}}})

	body := `{"model":"chat","messages":[{"role":"user","content":"hi"}]}`
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body)))

	if rec.Body.String() != body {
		t.Fatalf("body = %s, want it byte-identical when there is nothing to rewrite", rec.Body.String())
	}
}

func TestWithModelReplacesOnlyTheModel(t *testing.T) {
	body := []byte(`{"model":"chat","temperature":0.7,"tools":[{"type":"function"}],"n":2}`)
	out, err := withModel(body, "llama-tp2")
	if err != nil {
		t.Fatalf("withModel: %v", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(out, &fields); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if string(fields["model"]) != `"llama-tp2"` {
		t.Fatalf("model = %s", fields["model"])
	}
	for key, want := range map[string]string{
		"temperature": "0.7",
		"tools":       `[{"type":"function"}]`,
		"n":           "2",
	} {
		if string(fields[key]) != want {
			t.Fatalf("%s = %s, want %s untouched", key, fields[key], want)
		}
	}
}

func TestBothTokensAreAcceptedDuringARotation(t *testing.T) {
	guard := NewTokenGuard("old-secret,new-secret")
	for _, token := range []string{"old-secret", "new-secret"} {
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
		req.Header.Set("Authorization", "Bearer "+token)
		if !guard.Allow(req) {
			t.Fatalf("%s was refused; a rotation with no overlap is a coordinated flip", token)
		}
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	req.Header.Set("Authorization", "Bearer retired-secret")
	if guard.Allow(req) {
		t.Fatal("a token outside the set was accepted")
	}
}

func TestSeparatorsAndSpacingAreNotTokens(t *testing.T) {
	guard := NewTokenGuard(" old-secret , new-secret ,, ")
	for _, token := range []string{"old-secret", "new-secret"} {
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
		req.Header.Set("Authorization", "Bearer "+token)
		if !guard.Allow(req) {
			t.Fatalf("%s was refused; surrounding space must not be part of the secret", token)
		}
	}
	for _, empty := range []string{"", " "} {
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
		req.Header.Set("Authorization", "Bearer "+empty)
		if guard.Allow(req) {
			t.Fatalf("a trailing comma admitted %q as a token", empty)
		}
	}
	if NewTokenGuard(" , ,, ") != nil {
		t.Fatal("a value that is all separators must leave the gateway open, not admit nothing")
	}
}

// routeToDeployment is routeTo with the upstream carrying the deployment the
// plane resolved, which is what a real table always holds and what
// routing.NewUpstream — built for tests and for anything embedding the router —
// leaves empty.
func routeToDeployment(t *testing.T, address, deploymentID string) routing.Route {
	t.Helper()
	route := routeTo(t, address, nil)
	route.Upstreams[0].DeploymentID = deploymentID
	return route
}

// TestTheResolvedDeploymentIsForwardedAndACallersOwnIsNot is the security half
// of the header, and the half a happy-path test would miss.
//
// httputil.ReverseProxy copies the inbound headers onto the outbound request
// verbatim, so anything a caller sends under this name reaches the agent unless
// this hop overwrites it. The agent hands a request to the frontend the header
// names, so a caller who could set it would be choosing which model's frontend —
// which tenant's frontend — answers them, using a string they made up. The Set
// and the Del between them are what make the caller's value unreachable, and the
// Del is the case that is easy to leave out because it looks like a no-op.
func TestTheResolvedDeploymentIsForwardedAndACallersOwnIsNot(t *testing.T) {
	var seen http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	for _, tc := range []struct {
		name       string
		deployment string
		sent       string
		want       string
	}{
		{
			name:       "the upstream's deployment is stamped on",
			deployment: "dep_01j9x2",
			want:       "dep_01j9x2",
		},
		{
			name:       "a caller's own value is overwritten by the resolved one",
			deployment: "dep_01j9x2",
			sent:       "dep_somebody_elses",
			want:       "dep_01j9x2",
		},
		{
			name: "an upstream with no deployment deletes the caller's value rather than passing it",
			sent: "dep_somebody_elses",
			want: "",
		},
		{
			name: "nothing is added when there is nothing to say",
			want: "",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			seen = nil
			handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
				"chat": routeToDeployment(t, server.URL, tc.deployment),
			}}})

			req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
				strings.NewReader(`{"model":"chat"}`))
			if tc.sent != "" {
				req.Header.Set(tunnel.HeaderDeploymentID, tc.sent)
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)

			if rec.Code != http.StatusOK {
				t.Fatalf("status = %d, body = %s", rec.Code, rec.Body)
			}
			if got := seen.Get(tunnel.HeaderDeploymentID); got != tc.want {
				t.Fatalf("upstream saw %s = %q, want %q", tunnel.HeaderDeploymentID, got, tc.want)
			}
			if tc.want == "" {
				if _, present := seen[tunnel.HeaderDeploymentID]; present {
					t.Fatalf("the header is present as %v, want it absent rather than empty",
						seen[tunnel.HeaderDeploymentID])
				}
			}
		})
	}
}

// TestTheErrorClassFollowsTheStatus is what a client switches on to decide
// whether a retry is worth anything. Every error this hop writes and every one it
// substitutes for an upstream's takes its class from the same function, so a 503
// cannot arrive labelled as the caller's own bad request.
func TestTheErrorClassFollowsTheStatus(t *testing.T) {
	for status, want := range map[int]string{
		http.StatusBadRequest:            "invalid_request_error",
		http.StatusUnauthorized:          "authentication_error",
		http.StatusForbidden:             "permission_error",
		http.StatusNotFound:              "invalid_request_error",
		http.StatusRequestEntityTooLarge: "invalid_request_error",
		http.StatusTooManyRequests:       "rate_limit_error",
		http.StatusInternalServerError:   "api_error",
		http.StatusBadGateway:            "api_error",
		http.StatusServiceUnavailable:    "overloaded_error",
	} {
		rec := httptest.NewRecorder()
		writeError(rec, status, "code", "message")

		var envelope errorEnvelope
		if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
			t.Fatalf("decode: %v", err)
		}
		if envelope.Error.Type != want {
			t.Errorf("%d is type %q, want %q", status, envelope.Error.Type, want)
		}
	}
}

// TestTheGatewaysOwnRefusalsCarryTheRightClass walks the statuses through the
// real handler rather than the writer, because the classes only matter on the
// answers a caller actually receives.
func TestTheGatewaysOwnRefusalsCarryTheRightClass(t *testing.T) {
	handler := New(Config{
		Router:  stubResolver{routes: map[string]routing.Route{"chat": {}}},
		Callers: NewTokenGuard("secret"),
	})

	for _, test := range []struct {
		name   string
		token  string
		body   string
		status int
		want   string
	}{
		{name: "no token", body: `{"model":"chat"}`, status: http.StatusUnauthorized, want: "authentication_error"},
		{name: "unknown model", token: "secret", body: `{"model":"nope"}`, status: http.StatusNotFound, want: "invalid_request_error"},
		{name: "no upstream", token: "secret", body: `{"model":"chat"}`, status: http.StatusServiceUnavailable, want: "overloaded_error"},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(test.body))
			if test.token != "" {
				request.Header.Set("Authorization", "Bearer "+test.token)
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, request)

			if rec.Code != test.status {
				t.Fatalf("status = %d, want %d: %s", rec.Code, test.status, rec.Body)
			}
			var envelope errorEnvelope
			if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
				t.Fatalf("decode: %v", err)
			}
			if envelope.Error.Type != test.want {
				t.Fatalf("type = %q, want %q", envelope.Error.Type, test.want)
			}
		})
	}
}
