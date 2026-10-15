package serving

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

// refusalFor sends one completion body at an upstream that answers with the
// given status and body, and returns the error object the caller got.
func refusalFor(t *testing.T, status int, upstreamBody, callerBody string) (*httptest.ResponseRecorder, errorBody) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(upstreamBody))
	}))
	t.Cleanup(server.Close)
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"chat": routeTo(t, server.URL, nil),
	}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(callerBody)))

	var envelope errorEnvelope
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode %s: %v", rec.Body, err)
	}
	return rec, envelope.Error
}

// TestAnEngineSRefusalReachesTheCallerAsItsOwnStatus is the client report this was changed on:
// a bad input answered by the engine with a 4xx must come back as that 4xx and say what was
// wrong, in this hop's envelope with this hop's type and code.
func TestAnEngineSRefusalReachesTheCallerAsItsOwnStatus(t *testing.T) {
	for _, test := range []struct {
		name       string
		status     int
		upstream   string
		wantStatus int
		want       errorBody
	}{
		{
			name:       "the flat dynamo body",
			status:     http.StatusBadRequest,
			upstream:   `{"message":"Width must be divisible by 16 (got 1368)","type":"Bad Request","code":400}`,
			wantStatus: http.StatusBadRequest,
			want: errorBody{
				Message: "Width must be divisible by 16 (got 1368)", Type: "invalid_request_error", Code: "invalid_request",
			},
		},
		{
			name:       "the OpenAI envelope",
			status:     http.StatusBadRequest,
			upstream:   `{"error":{"message":"Unsupported field: seed_mode","type":"BadRequestError","code":400}}`,
			wantStatus: http.StatusBadRequest,
			want: errorBody{
				Message: "Unsupported field: seed_mode", Type: "invalid_request_error", Code: "invalid_request",
			},
		},
		{
			name:       "a FastAPI validation list",
			status:     http.StatusUnprocessableEntity,
			upstream:   `{"detail":[{"loc":["body","size"],"msg":"size must look like 1024x1024","type":"value_error"}]}`,
			wantStatus: http.StatusBadRequest,
			want: errorBody{
				Message: "size must look like 1024x1024", Type: "invalid_request_error", Code: "invalid_request",
			},
		},
		{
			name:       "a 4xx nobody anticipated keeps its status",
			status:     http.StatusConflict,
			upstream:   `{"message":"request already in progress"}`,
			wantStatus: http.StatusConflict,
			want: errorBody{
				Message: "request already in progress", Type: "invalid_request_error", Code: "invalid_request",
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			rec, got := refusalFor(t, test.status, test.upstream, `{"model":"chat","messages":[]}`)
			if rec.Code != test.wantStatus {
				t.Fatalf("status = %d, want %d", rec.Code, test.wantStatus)
			}
			if got != test.want {
				t.Fatalf("error = %#v, want %#v", got, test.want)
			}
		})
	}
}

// TestAServerFailureIsNeverQuoted: a 5xx happened behind this hop, so it is answered as this
// hop's own 502 and nothing the engine said reaches the caller, however it was shaped.
func TestAServerFailureIsNeverQuoted(t *testing.T) {
	for _, upstream := range []string{
		`{"message":"Failed to generate images","type":"Internal Server Error","code":500}`,
		`{"message":"ValueError: invalid video file","type":"Internal Server Error","code":500}`,
		`{"error":{"message":"temperature handler panicked"}}`,
		`{"message":"RuntimeError: CUDA out of memory\nTraceback (most recent call last):"}`,
		`Internal Server Error`,
	} {
		rec, got := refusalFor(t, http.StatusInternalServerError, upstream, `{"model":"chat","temperature":1}`)
		want := errorBody{Message: "The model failed to answer this request.", Type: "api_error", Code: "upstream_error"}
		if rec.Code != http.StatusBadGateway || got != want {
			t.Fatalf("%s: status = %d, error = %#v, want 502 %#v", upstream, rec.Code, got, want)
		}
	}
}

// TestAQuotedMessageNamesNothingBehindThisHop is the half of quoting that is not negotiable: the
// engine's words may reach a caller, the address of the thing that said them may not.
func TestAQuotedMessageNamesNothingBehindThisHop(t *testing.T) {
	for _, test := range []struct {
		name   string
		status int
		detail string
		never  []string
	}{
		{
			name:   "a cluster-internal service name",
			status: http.StatusBadRequest,
			detail: "kv_cache_block_size rejected by model-frontend.dynamo.svc.cluster.local:8000",
			never:  []string{"model-frontend", "svc.cluster.local", "8000"},
		},
		{
			name:   "a URL and an address",
			status: http.StatusBadRequest,
			detail: "connect to http://10.100.4.17:9345/generate failed; peer 10.100.4.18:9345",
			never:  []string{"10.100.4.17", "10.100.4.18", "http://", "/generate"},
		},
		{
			name:   "a word that names the stack",
			status: http.StatusBadRequest,
			detail: "vllm_omni rejected the request: see ghcr.io/hangar-dev/engine and Dynamo docs",
			never:  []string{"vllm", "ghcr.io", "Dynamo"},
		},
		{
			name:   "a source file and a filesystem path",
			status: http.StatusBadRequest,
			detail: "error in /usr/local/lib/python3.12/site-packages/vllm_omni/diffusion.py:212 loading /models/hub/wan",
			never:  []string{"/usr/local", "site-packages", "diffusion.py", "/models/hub"},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			body, _ := json.Marshal(map[string]string{"message": test.detail})
			rec, got := refusalFor(t, test.status, string(body), `{"model":"chat"}`)
			for _, secret := range test.never {
				if strings.Contains(rec.Body.String(), secret) {
					t.Fatalf("body = %s, it still carries %q", rec.Body, secret)
				}
			}
			if !strings.Contains(got.Message, redacted) {
				t.Fatalf("message = %q, want the address replaced rather than the message dropped", got.Message)
			}
		})
	}
}

// TestAnOperatorsOwnNamesAreRedacted so that a deployment can add the names its own infrastructure
// goes by without patching the gateway.
func TestAnOperatorsOwnNamesAreRedacted(t *testing.T) {
	t.Cleanup(func() { RedactNames(nil) })
	message := "rejected by example-corp-edge-2 (contact ops@example-corp)"
	if got := sanitizeUpstreamMessage(message, servedName{}); !strings.Contains(got, "example-corp") {
		t.Fatalf("message = %q, an unconfigured name should pass through", got)
	}
	RedactNames([]string{" example-corp ", ""})
	got := sanitizeUpstreamMessage(message, servedName{})
	if strings.Contains(got, "example-corp") || !strings.Contains(got, redacted) {
		t.Fatalf("message = %q, want example-corp redacted", got)
	}
	if got := sanitizeUpstreamMessage("vllm said no", servedName{}); strings.Contains(got, "vllm") {
		t.Fatalf("message = %q, the built-in names must still apply", got)
	}
}

// TestAQuotedMessageIsCapped so that an engine's message is never a dump.
func TestAQuotedMessageIsCapped(t *testing.T) {
	long := strings.Repeat("é bad size ", 200)
	got := sanitizeUpstreamMessage(long, servedName{})
	if len(got) > maxQuotedUpstreamLen+len("…") || !strings.HasSuffix(got, "…") {
		t.Fatalf("len = %d, message = %q, want at most %d bytes and an ellipsis", len(got), got, maxQuotedUpstreamLen)
	}
	if !utf8.ValidString(got) {
		t.Fatalf("message = %q, the cap split a rune", got)
	}
}

// TestAnEngineSServedNameReadsBackAsTheCallersModel because the name the engine answers to is
// ours; the caller asked for the public one.
func TestAnEngineSServedNameReadsBackAsTheCallersModel(t *testing.T) {
	got := sanitizeUpstreamMessage("model Qwen/Qwen-Image-2512 does not support n > 1",
		servedName{served: "Qwen/Qwen-Image-2512", public: "initech-image"})
	if got != "model initech-image does not support n > 1" {
		t.Fatalf("message = %q", got)
	}
}

// TestAKnownRefusalIsAnsweredWithOurOwnMessage is a reason the engine gave that
// this hop recognises: it selects a fixed message, and only on a 4xx.
func TestAKnownRefusalIsAnsweredWithOurOwnMessage(t *testing.T) {
	rec, got := refusalFor(t, http.StatusBadRequest,
		`{"message":"ValueError: invalid video file (codec h266 at /tmp/x.mp4)","type":"Bad Request","code":400}`,
		`{"model":"chat","messages":[]}`)
	want := errorBody{Message: "invalid video file", Type: "invalid_request_error", Code: "invalid_request"}
	if rec.Code != http.StatusBadRequest || got != want {
		t.Fatalf("status = %d, error = %#v, want 400 %#v", rec.Code, got, want)
	}
}

// TestARefusalNamesTheCallerSOwnFieldWhenTheEngineComplainedAboutIt is the fallback when there is
// no message to quote: a prose 4xx says nothing quotable, so a field the caller themselves sent is
// reported back when the engine's complaint mentions it.
func TestARefusalNamesTheCallerSOwnFieldWhenTheEngineComplainedAboutIt(t *testing.T) {
	_, got := refusalFor(t, http.StatusBadRequest,
		`temperature must be in [0, 2], got 9000`,
		`{"model":"chat","temperature":9000}`)

	if got.Message != "The request was not accepted by this model. Check these fields: temperature." {
		t.Fatalf("message = %q, want the caller's own field named and the prose left out", got.Message)
	}
}

// TestOnlyTheCallerSOwnFieldNamesCanBeEchoed keeps that fallback's alphabet closed: from prose,
// only tokens that arrived in this very request can come back.
func TestOnlyTheCallerSOwnFieldNamesCanBeEchoed(t *testing.T) {
	for _, test := range []struct {
		name     string
		upstream string
		caller   string
		wantOut  string
	}{
		{
			name:     "a name only the engine knows",
			upstream: `kv_cache_block_size rejected by model-frontend.dynamo.svc.cluster.local`,
			caller:   `{"model":"chat","temperature":1}`,
			wantOut:  "kv_cache_block_size",
		},
		{
			name:     "a caller field that is a prefix of the engine's word",
			upstream: `stop_token_ids is not supported`,
			caller:   `{"model":"chat","stop":["x"]}`,
			wantOut:  "stop_token_ids",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, got := refusalFor(t, http.StatusBadRequest, test.upstream, test.caller)
			if strings.Contains(got.Message, test.wantOut) {
				t.Fatalf("message = %q, it carries %q which the caller never sent", got.Message, test.wantOut)
			}
			if got.Message != "The request was not accepted by this model." {
				t.Fatalf("message = %q, want the generic sentence and nothing appended", got.Message)
			}
		})
	}
}

// TestACredentialOrRoutingFailureQuotesNothing: an upstream 401, 403 or 404 is our own
// misconfiguration answered as 502, and what the engine said about our credential or our served
// name is not the caller's business.
func TestACredentialOrRoutingFailureQuotesNothing(t *testing.T) {
	for _, status := range []int{http.StatusUnauthorized, http.StatusForbidden, http.StatusNotFound} {
		rec, got := refusalFor(t, status, `{"message":"token abc123 rejected for model qwen-internal"}`, `{"model":"chat"}`)
		if rec.Code != http.StatusBadGateway || got.Message != "The model failed to answer this request." {
			t.Fatalf("%d: status = %d, message = %q", status, rec.Code, got.Message)
		}
	}
}

func TestATokenIsOnlyMatchedWholeInAnUpstreamMessage(t *testing.T) {
	for _, test := range []struct {
		text string
		name string
		want bool
	}{
		{"invalid value for tool_choice", "tool_choice", true},
		{"invalid value for tool_choice", "tool", false},
		{"field 'top_p' out of range", "top_p", true},
		{"the temperatures given", "temperature", false},
		{"temperature: bad", "temperature", true},
	} {
		if got := mentionsToken(test.text, test.name); got != test.want {
			t.Fatalf("mentionsToken(%q, %q) = %v, want %v", test.text, test.name, got, test.want)
		}
	}
}

// TestAQuotedRefusalNamesTheModelTheCallerAskedFor is the served-name rewrite read back through
// the whole hop, not only the sanitiser.
func TestAQuotedRefusalNamesTheModelTheCallerAskedFor(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"message":"Qwen/Qwen-Image-2512 does not support n > 1","type":"Bad Request","code":400}`))
	}))
	t.Cleanup(server.Close)
	route := routeTo(t, server.URL, nil)
	route.Upstreams[0].ServedModelName = "Qwen/Qwen-Image-2512"
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{"chat": route}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"chat","n":2}`)))

	var envelope errorEnvelope
	_ = json.Unmarshal(rec.Body.Bytes(), &envelope)
	if rec.Code != http.StatusBadRequest || strings.Contains(rec.Body.String(), "Qwen") ||
		envelope.Error.Message != "chat does not support n > 1" {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body)
	}
}
