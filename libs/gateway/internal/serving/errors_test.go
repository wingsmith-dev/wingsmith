package serving

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

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
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
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

// TestAnEngineSInternalTypeNamesCannotReachACaller is the defect this hop was
// measured on: a serde failure over an untagged enum, answered verbatim, which
// published a Rust type and a Rust error mode to a customer and called the class
// "Bad Request".
func TestAnEngineSInternalTypeNamesCannotReachACaller(t *testing.T) {
	rec, got := refusalFor(t, http.StatusBadRequest,
		`{"error":{"message":"Failed to deserialize the JSON body into the target type: `+
			`data did not match any variant of untagged enum ChatCompletionToolChoiceOption `+
			`at line 1 column 452","type":"Bad Request","code":400}}`,
		`{"model":"chat","tool_choice":{"type":"allowed_tools"}}`)

	for _, internal := range []string{
		"ChatCompletionToolChoiceOption", "untagged enum", "deserialize", "Bad Request",
	} {
		if strings.Contains(rec.Body.String(), internal) {
			t.Fatalf("body = %s, it still carries %q", rec.Body, internal)
		}
	}
	want := errorBody{
		Message: "The request was not accepted by this model.",
		Type:    "invalid_request_error",
		Code:    "invalid_request",
	}
	if got != want {
		t.Fatalf("error = %#v, want %#v", got, want)
	}
}

// TestAKnownRefusalIsAnsweredWithOurOwnMessage is a reason the engine gave that
// this hop recognises: it selects a fixed message, and only on a 4xx.
func TestAKnownRefusalIsAnsweredWithOurOwnMessage(t *testing.T) {
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
			upstream:   `{"message":"ValueError: invalid video file","type":"Bad Request","code":400}`,
			wantStatus: http.StatusBadRequest,
			want: errorBody{
				Message: "invalid video file", Type: "invalid_request_error", Code: "invalid_request",
			},
		},
		{
			name:       "any other 4xx body",
			status:     http.StatusBadRequest,
			upstream:   `{"message":"ValueError: invalid audio file","type":"Bad Request","code":400}`,
			wantStatus: http.StatusBadRequest,
			want: errorBody{
				Message: "The request was not accepted by this model.",
				Type:    "invalid_request_error", Code: "invalid_request",
			},
		},
		{
			name:       "a 5xx that says the same thing",
			status:     http.StatusInternalServerError,
			upstream:   `{"message":"ValueError: invalid video file","type":"Internal Server Error","code":500}`,
			wantStatus: http.StatusBadGateway,
			want: errorBody{
				Message: "The model failed to answer this request.",
				Type:    "api_error", Code: "upstream_error",
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
			if strings.Contains(rec.Body.String(), "ValueError") {
				t.Fatalf("body = %s, the engine's own words were quoted", rec.Body)
			}
		})
	}
}

// TestARefusalNamesTheCallerSOwnFieldWhenTheEngineComplainedAboutIt is the other
// half of the line: a 4xx that says nothing at all is a 4xx nobody can debug, so
// a field the caller themselves sent is reported back when the engine's own
// complaint mentions it.
func TestARefusalNamesTheCallerSOwnFieldWhenTheEngineComplainedAboutIt(t *testing.T) {
	_, got := refusalFor(t, http.StatusBadRequest,
		`{"object":"error","message":"temperature must be in [0, 2], got 9000"}`,
		`{"model":"chat","temperature":9000}`)

	if !strings.Contains(got.Message, "Check these fields: temperature.") {
		t.Fatalf("message = %q, the caller's own field was destroyed with the engine's prose", got.Message)
	}
	if strings.Contains(got.Message, "9000") || strings.Contains(got.Message, "must be in") {
		t.Fatalf("message = %q, the engine's own words were quoted", got.Message)
	}
}

// TestOnlyTheCallerSOwnFieldNamesCanBeEchoed is what makes the leak class
// impossible rather than filtered: the message's alphabet is fixed strings plus
// tokens that arrived in this very request, so an engine's tuning parameter,
// module path or service name has nowhere to come from.
func TestOnlyTheCallerSOwnFieldNamesCanBeEchoed(t *testing.T) {
	for _, test := range []struct {
		name     string
		upstream string
		caller   string
		wantOut  string
	}{
		{
			name:     "a name only the engine knows",
			upstream: `{"message":"kv_cache_block_size rejected by kimi-k3-frontend.dynamo.svc.cluster.local"}`,
			caller:   `{"model":"chat","temperature":1}`,
			wantOut:  "kv_cache_block_size",
		},
		{
			name:     "a caller field that is a prefix of the engine's word",
			upstream: `{"message":"stop_token_ids is not supported"}`,
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

// TestAServerFailureNeverNamesACallerSField because a 5xx is this hop's problem
// and pointing at the caller's parameters would send them to debug ours.
func TestAServerFailureNeverNamesACallerSField(t *testing.T) {
	_, got := refusalFor(t, http.StatusInternalServerError,
		`{"message":"temperature handler panicked"}`,
		`{"model":"chat","temperature":1}`)

	if got.Message != "The model failed to answer this request." {
		t.Fatalf("message = %q, want the 5xx sentence alone", got.Message)
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
