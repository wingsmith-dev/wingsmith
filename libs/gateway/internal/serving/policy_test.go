package serving

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

func number(value float64) *float64 { return &value }

// kimiPolicy is the shape the Moonshot comparison produced: every pinned
// parameter is an enum of one.
func kimiPolicy() *plane.ParameterPolicy {
	return &plane.ParameterPolicy{
		Version: 1,
		Parameters: map[string]plane.ParameterRule{
			"temperature":       {Enum: []any{float64(1)}},
			"top_p":             {Enum: []any{0.95}},
			"presence_penalty":  {Enum: []any{float64(0)}},
			"frequency_penalty": {Enum: []any{float64(0)}},
			"logprobs":          {Enum: []any{false}},
		},
		Deny: []string{"tool_choice.named_while_thinking"},
	}
}

func TestNilPolicyAllowsEverything(t *testing.T) {
	body := []byte(`{"model":"m","temperature":0.7,"tool_choice":{"type":"function"}}`)
	if _, _, ok := checkPolicy(nil, body); !ok {
		t.Fatal("a nil policy must not reject anything")
	}
}

func TestPinnedParameterRejectsOtherValues(t *testing.T) {
	for _, body := range []string{
		`{"model":"m","temperature":0.7}`,
		`{"model":"m","top_p":0.9}`,
		`{"model":"m","presence_penalty":0.5}`,
		`{"model":"m","logprobs":true}`,
	} {
		code, _, ok := checkPolicy(kimiPolicy(), []byte(body))
		if ok {
			t.Fatalf("%s: expected a violation", body)
		}
		if code != "invalid_parameter" {
			t.Fatalf("%s: code = %q", body, code)
		}
	}
}

func TestPinnedParameterAcceptsTheOneValue(t *testing.T) {
	body := []byte(`{"model":"m","temperature":1,"top_p":0.95,"presence_penalty":0,"logprobs":false}`)
	if _, _, ok := checkPolicy(kimiPolicy(), body); !ok {
		t.Fatal("the pinned values must be accepted")
	}
}

// An absent parameter is not a violation. The provider rejects a bad value; it
// does not demand the field, and requiring it would refuse the minimal request
// every client starts with.
func TestAbsentParameterIsNotAViolation(t *testing.T) {
	body := []byte(`{"model":"m","messages":[]}`)
	if _, _, ok := checkPolicy(kimiPolicy(), body); !ok {
		t.Fatal("a request that sends nothing must pass a pinning policy")
	}
}

// The matrix measured against api.moonshot.ai/kimi-k3 on 2026-09-04. Exactly
// one cell is refused, and the two easy over-approximations of it — "an object
// tool_choice" and "a named function" — both refuse requests the provider
// serves.
func TestNamedToolChoiceIsRefusedOnlyWhileThinking(t *testing.T) {
	const (
		named   = `{"type":"function","function":{"name":"f"}}`
		allowed = `{"type":"allowed_tools","allowed_tools":{"mode":"auto","tools":[]}}`

		thinkingOn     = `` // absent: K3 thinks unless it is told not to
		thinkingOff    = `,"thinking":{"type":"disabled"}`
		thinkingOnLoud = `,"thinking":{"type":"enabled"}`
		templateOff    = `,"chat_template_kwargs":{"thinking":false}`
		templateOn     = `,"chat_template_kwargs":{"thinking":true}`
	)

	for _, tc := range []struct {
		name     string
		choice   string
		thinking string
		refused  bool
	}{
		{"named, thinking left to the default", named, thinkingOn, true},
		{"named, thinking asked for", named, thinkingOnLoud, true},
		{"named, thinking switched off", named, thinkingOff, false},
		{"named, thinking off via the template", named, templateOff, false},
		{"named, thinking on via the template", named, templateOn, true},

		// An object, but not the one the provider refuses.
		{"allowed_tools, thinking on", allowed, thinkingOn, false},
		{"allowed_tools, thinking off", allowed, thinkingOff, false},

		// The bare strings are a different feature and are never this rule.
		{"auto", `"auto"`, thinkingOn, false},
		{"none", `"none"`, thinkingOn, false},
		{"required", `"required"`, thinkingOn, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body := []byte(`{"model":"m","tool_choice":` + tc.choice + tc.thinking + `}`)
			code, _, ok := checkPolicy(kimiPolicy(), body)
			if ok == tc.refused {
				t.Fatalf("refused = %v, want %v", !ok, tc.refused)
			}
			if tc.refused && code != "unsupported_parameter" {
				t.Fatalf("code = %q", code)
			}
		})
	}
}

// A body this rule cannot read is the upstream's problem, not a reason to drop
// it here. Every one of these would answer 400 upstream anyway, and refusing
// them locally would only move the error and lose the provider's reason for it.
func TestUnreadableThinkingIsLeftToTheUpstream(t *testing.T) {
	for _, body := range []string{
		`{"model":"m","tool_choice":{"type":"function"},"thinking":"enabled"}`,
		`{"model":"m","tool_choice":{"type":"function"},"chat_template_kwargs":[]}`,
		`{"model":"m","tool_choice":[]}`,
		`{"model":"m","tool_choice":null}`,
	} {
		if _, _, ok := checkPolicy(kimiPolicy(), []byte(body)); !ok {
			t.Fatalf("%s: a shape this rule cannot read must not be refused here", body)
		}
	}
}

func TestRangeRule(t *testing.T) {
	policy := &plane.ParameterPolicy{
		Version:    1,
		Parameters: map[string]plane.ParameterRule{"temperature": {Min: number(0), Max: number(2)}},
	}
	if _, _, ok := checkPolicy(policy, []byte(`{"temperature":1.5}`)); !ok {
		t.Fatal("in range must pass")
	}
	if _, _, ok := checkPolicy(policy, []byte(`{"temperature":2.5}`)); ok {
		t.Fatal("above max must fail")
	}
	if _, _, ok := checkPolicy(policy, []byte(`{"temperature":"hot"}`)); ok {
		t.Fatal("a non-number must fail a numeric range")
	}
}

// A rule this build cannot read must not drop traffic. The gap is reported at
// refresh time instead.
func TestUnknownDenialIsIgnored(t *testing.T) {
	policy := &plane.ParameterPolicy{Version: 99, Deny: []string{"something.invented"}}
	if _, _, ok := checkPolicy(policy, []byte(`{"model":"m"}`)); !ok {
		t.Fatal("an unreadable rule must not reject the request")
	}
}

func TestViolationIsStableAcrossRuns(t *testing.T) {
	body := []byte(`{"temperature":0.7,"top_p":0.9,"logprobs":true}`)
	_, first, _ := checkPolicy(kimiPolicy(), body)
	for i := 0; i < 50; i++ {
		if _, message, _ := checkPolicy(kimiPolicy(), body); message != first {
			t.Fatalf("violation is unstable: %q then %q", first, message)
		}
	}
}

// The policy must survive the plane's own JSON, not just a hand-built struct.
func TestPolicyDecodesFromPlaneJSON(t *testing.T) {
	raw := `{"version":1,"parameters":{"temperature":{"enum":[1]},"n":{"min":1,"max":1}},"deny":["tool_choice.named_while_thinking"]}`
	var policy plane.ParameterPolicy
	if err := json.Unmarshal([]byte(raw), &policy); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if _, _, ok := checkPolicy(&policy, []byte(`{"temperature":0.7}`)); ok {
		t.Fatal("decoded policy must still reject")
	}
	if _, _, ok := checkPolicy(&policy, []byte(`{"temperature":1,"n":1}`)); !ok {
		t.Fatal("decoded policy must still accept the pinned values")
	}
}

// End to end: the policy has to reach the request path, refuse before the
// upstream is dialled, and leave every other endpoint untouched.
func TestPolicyRejectsBeforeReachingUpstream(t *testing.T) {
	server, _ := upstreamServer(t)

	pinned := routeTo(t, server.URL, nil)
	pinned.Policy = kimiPolicy()
	open := routeTo(t, server.URL, nil)

	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{
		"pinned": pinned,
		"open":   open,
	}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"pinned","temperature":0.7}`)))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("pinned endpoint: status = %d, body = %s", rec.Code, rec.Body.String())
	}

	var envelope struct {
		Error struct {
			Code string `json:"code"`
			Type string `json:"type"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("error body is not the OpenAI envelope: %v", err)
	}
	if envelope.Error.Code != "invalid_parameter" {
		t.Fatalf("code = %q", envelope.Error.Code)
	}

	// The same request on an endpoint that sold no terms is untouched. This is
	// the property the whole per-endpoint design exists for.
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"open","temperature":0.7}`)))
	if rec.Code != http.StatusOK {
		t.Fatalf("open endpoint: status = %d, body = %s", rec.Code, rec.Body.String())
	}
}
