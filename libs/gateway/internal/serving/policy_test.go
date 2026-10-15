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

func boolean(value bool) *bool { return &value }

// The paths this gateway fronts, named so that a test says which protocol it is
// about. A policy is attached to an endpoint and an endpoint answers on all of
// them, which is the whole reason a rule carries a route.
const (
	chatRoute       = "/v1/chat/completions"
	embeddingsRoute = "/v1/embeddings"
	messagesRoute   = "/v1/messages"
	responsesRoute  = "/v1/responses"
)

// kimiPolicy is the shape the Moonshot comparison produced: every pinned
// parameter is an enum of one, and every one of them was measured against chat
// completions and now says so.
func kimiPolicy() *plane.Policy {
	pinned := func(key string, value any) plane.Rule {
		return plane.Rule{Route: chatRoute, Key: key, Enum: []any{value}}
	}
	return &plane.Policy{
		Version: 2,
		Rules: []plane.Rule{
			pinned("temperature", float64(1)),
			pinned("top_p", 0.95),
			pinned("presence_penalty", float64(0)),
			pinned("frequency_penalty", float64(0)),
			pinned("logprobs", false),
			{Route: chatRoute, Predicate: "tool_choice.named_while_thinking", Deny: true},
		},
	}
}

func TestNilPolicyAllowsEverything(t *testing.T) {
	body := []byte(`{"model":"m","temperature":0.7,"tool_choice":{"type":"function"}}`)
	if _, _, ok := checkPolicy(nil, chatRoute, body); !ok {
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
		code, _, ok := checkPolicy(kimiPolicy(), chatRoute, []byte(body))
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
	if _, _, ok := checkPolicy(kimiPolicy(), chatRoute, body); !ok {
		t.Fatal("the pinned values must be accepted")
	}
}

// An absent parameter is not a violation. The provider rejects a bad value; it
// does not demand the field, and requiring it would refuse the minimal request
// every client starts with.
func TestAbsentParameterIsNotAViolation(t *testing.T) {
	body := []byte(`{"model":"m","messages":[]}`)
	if _, _, ok := checkPolicy(kimiPolicy(), chatRoute, body); !ok {
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
		// The template spelling is not read. Moonshot has no
		// `chat_template_kwargs`, ignores it and thinks anyway, so a request
		// carrying it is in the refused cell there whatever it says. The
		// endpoint's policy drops the field before the engine sees it, which is
		// what keeps this reading and the engine's behaviour the same fact.
		{"named, thinking off via the template", named, templateOff, true},
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
			code, _, ok := checkPolicy(kimiPolicy(), chatRoute, body)
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
		`{"model":"m","tool_choice":[]}`,
		`{"model":"m","tool_choice":null}`,
	} {
		if _, _, ok := checkPolicy(kimiPolicy(), chatRoute, []byte(body)); !ok {
			t.Fatalf("%s: a shape this rule cannot read must not be refused here", body)
		}
	}
}

func TestRangeRule(t *testing.T) {
	policy := &plane.Policy{
		Version: 2,
		Rules:   []plane.Rule{{Key: "temperature", Min: number(0), Max: number(2)}},
	}
	if _, _, ok := checkPolicy(policy, chatRoute, []byte(`{"temperature":1.5}`)); !ok {
		t.Fatal("in range must pass")
	}
	if _, _, ok := checkPolicy(policy, chatRoute, []byte(`{"temperature":2.5}`)); ok {
		t.Fatal("above max must fail")
	}
	if _, _, ok := checkPolicy(policy, chatRoute, []byte(`{"temperature":"hot"}`)); ok {
		t.Fatal("a non-number must fail a numeric range")
	}
}

// A rule this build cannot read must not drop traffic. The gap is reported at
// refresh time instead.
func TestUnknownDenialIsIgnored(t *testing.T) {
	policy := &plane.Policy{Version: 99, Rules: []plane.Rule{{Predicate: "something.invented", Deny: true}}}
	if _, _, ok := checkPolicy(policy, chatRoute, []byte(`{"model":"m"}`)); !ok {
		t.Fatal("an unreadable rule must not reject the request")
	}
}

func TestViolationIsStableAcrossRuns(t *testing.T) {
	body := []byte(`{"temperature":0.7,"top_p":0.9,"logprobs":true}`)
	_, first, _ := checkPolicy(kimiPolicy(), chatRoute, body)
	for i := 0; i < 50; i++ {
		if _, message, _ := checkPolicy(kimiPolicy(), chatRoute, body); message != first {
			t.Fatalf("violation is unstable: %q then %q", first, message)
		}
	}
}

// The policy must survive the plane's own JSON, not just a hand-built struct.
func TestPolicyDecodesFromPlaneJSON(t *testing.T) {
	raw := `{"version":2,"rules":[` +
		`{"route":"/v1/chat/completions","key":"temperature","enum":[1]},` +
		`{"route":"/v1/chat/completions","key":"n","min":1,"max":1},` +
		`{"route":"/v1/chat/completions","predicate":"tool_choice.named_while_thinking","deny":true}]}`
	var policy plane.Policy
	if err := json.Unmarshal([]byte(raw), &policy); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if _, _, ok := checkPolicy(&policy, chatRoute, []byte(`{"temperature":0.7}`)); ok {
		t.Fatal("decoded policy must still reject")
	}
	if _, _, ok := checkPolicy(&policy, chatRoute, []byte(`{"temperature":1,"n":1}`)); !ok {
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

	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
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

// refusedBodies are the request payloads the conformance board sends for the
// seventeen `messages[].tools` rows, transcribed out of
// api/checks/dynamic_tools.py byte for byte and keyed by the row each one
// belongs to.
//
// Transcribed rather than paraphrased, because a hand-written approximation of
// a wire format is exactly how a rule passes its own tests and then refuses
// the provider's traffic. If a row moves, this block is what has to move with
// it.
var refusedBodies = map[string]string{
	"dynamic_tools.user_message_rejected":               `{"messages":[{"role":"user","content":"hi","tools":[{"type":"function","function":{"name":"get_weather","description":"Get the weather of a city.","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]},{"role":"user","content":"what is the weather in beijing?"}]}`,
	"dynamic_tools.assistant_message_rejected":          `{"messages":[{"role":"user","content":"hi"},{"role":"assistant","content":"hello","tools":[{"type":"function","function":{"name":"get_weather","description":"Get the weather of a city.","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]},{"role":"user","content":"what is the weather in beijing?"}]}`,
	"dynamic_tools.nonempty_content_rejected":           `{"messages":[{"role":"system","content":"not empty","tools":[{"type":"function","function":{"name":"get_weather","description":"Get the weather of a city.","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.missing_type_rejected":               `{"messages":[{"role":"system","content":"","tools":[{"function":{"name":"good_tool","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.missing_function_rejected":           `{"messages":[{"role":"system","content":"","tools":[{"type":"function"}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.missing_name_rejected":               `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.invalid_name_leading_digit_rejected": `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"1bad_name","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.invalid_name_special_char_rejected":  `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"bad@name","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.invalid_name_empty_rejected":         `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.invalid_name_too_long_257_rejected":  `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.bogus_type_rejected":                 `{"messages":[{"role":"system","content":"","tools":[{"type":"bogus","function":{"name":"x"}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.mixed_valid_bogus_rejected":          `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"good_tool","description":"A tool.","parameters":{"type":"object","properties":{}}}},{"type":"bogus","function":{"name":"x"}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.not_array_rejected":                  `{"messages":[{"role":"system","content":"","tools":{"type":"function"}},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.item_not_object_rejected":            `{"messages":[{"role":"system","content":"","tools":[null]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.duplicate_in_message_rejected":       `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"dup","description":"A tool.","parameters":{"type":"object","properties":{}}}},{"type":"function","function":{"name":"dup","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.duplicate_across_messages_rejected":  `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"dup","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"system","content":"","tools":[{"type":"function","function":{"name":"dup","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"hello"}]}`,
	"dynamic_tools.duplicate_global_dynamic_rejected":   `{"tools":[{"type":"function","function":{"name":"dup","description":"A tool.","parameters":{"type":"object","properties":{}}}}],"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"dup","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"hello"}]}`,
}

// servedBodies are the six payloads the same board expects a 200 from. Every
// one of them is a declaration K3 serves, so a rule that refuses one has
// replaced a wrong answer with a broken request, which is worse.
var servedBodies = map[string]string{
	"dynamic_tools.callable":             `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"get_weather","description":"Get the weather of a city.","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]},{"role":"user","content":"what is the weather in beijing?"}],"tool_choice":"required"}`,
	"dynamic_tools.subsequent_system":    `{"messages":[{"role":"system","content":"You are a helpful assistant."},{"role":"user","content":"hi"},{"role":"assistant","content":"Hello!"},{"role":"system","content":"","tools":[{"type":"function","function":{"name":"get_weather","description":"Get the weather of a city.","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]},{"role":"user","content":"what is the weather in beijing?"}],"tool_choice":"required"}`,
	"dynamic_tools.as_last_message":      `{"messages":[{"role":"user","content":"what is the weather in beijing?"},{"role":"system","content":"","tools":[{"type":"function","function":{"name":"get_weather","description":"Get the weather of a city.","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}],"tool_choice":"required"}`,
	"dynamic_tools.three_in_one_message": `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"get_weather","description":"A tool.","parameters":{"type":"object","properties":{}}}},{"type":"function","function":{"name":"get_time","description":"A tool.","parameters":{"type":"object","properties":{}}}},{"type":"function","function":{"name":"get_news","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"what is the weather in beijing?"}],"tool_choice":"required"}`,
	"dynamic_tools.two_systems":          `{"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"get_weather","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"system","content":"","tools":[{"type":"function","function":{"name":"get_time","description":"A tool.","parameters":{"type":"object","properties":{}}}}]},{"role":"user","content":"what is the weather in beijing?"}],"tool_choice":"required"}`,
	"dynamic_tools.coexist_with_global":  `{"tools":[{"type":"function","function":{"name":"get_stock_price","description":"Get the stock price of a company.","parameters":{"type":"object","properties":{}}}}],"messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"get_weather","description":"Get the weather of a city.","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]},{"role":"user","content":"what is the weather in beijing?"}],"tool_choice":"required"}`,
}

// genericRefusal is the sentence `denials` answers with. It is repeated here
// rather than imported so that the contrast is visible: the whole reason
// `messages.tools` sits in the other registry is that a customer holding a
// malformed declaration cannot do anything with this sentence.
const genericRefusal = "The request uses a feature this model does not accept."

// dynamicToolsPolicy is what an endpoint selling K3 conformance carries once
// message-level declarations are enforced: the pinned parameters, the
// tool_choice denial, and the new rule.
func dynamicToolsPolicy() *plane.Policy {
	policy := kimiPolicy()
	policy.Rules = append(policy.Rules,
		plane.Rule{Route: chatRoute, Predicate: "messages.tools", Deny: true})
	return policy
}

func withModelNamed(name, body string) string {
	return `{"model":"` + name + `",` + body[1:]
}

func jsonQuoted(value string) string {
	encoded, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return string(encoded)
}

// Every row the board expects a 400 from. The count is asserted because the
// board growing a row this build has never seen is the failure mode that would
// otherwise pass silently.
func TestBoardRejectionsAreRefused(t *testing.T) {
	if len(refusedBodies) != 17 {
		t.Fatalf("fixtures = %d, want the board's 17", len(refusedBodies))
	}
	for id, body := range refusedBodies {
		t.Run(id, func(t *testing.T) {
			code, message, ok := checkPolicy(dynamicToolsPolicy(), chatRoute, []byte(body))
			if ok {
				t.Fatal("the provider answers this 400 and this hop must too")
			}
			if code != "invalid_parameter" {
				t.Fatalf("code = %q", code)
			}
			if message == genericRefusal {
				t.Fatal("a malformed declaration reported as an unsupported feature tells the caller to stop using something they are entitled to")
			}
			if !strings.Contains(message, "messages[") {
				t.Fatalf("the message must say which declaration is wrong: %q", message)
			}
		})
	}
}

// Every row the board expects a 200 from. These are the requests K3 serves, so
// refusing one here would replace a wrong answer with a broken request, which
// is the worse of the two.
func TestBoardAcceptancesAreServed(t *testing.T) {
	if len(servedBodies) != 6 {
		t.Fatalf("fixtures = %d, want the board's 6", len(servedBodies))
	}
	for id, body := range servedBodies {
		t.Run(id, func(t *testing.T) {
			if _, message, ok := checkPolicy(dynamicToolsPolicy(), chatRoute, []byte(body)); !ok {
				t.Fatalf("the provider serves this: %s", message)
			}
		})
	}
}

// The rule reaches a request only through the name in the policy. An endpoint
// that sold nothing, and one that sold other terms, both have to keep serving
// every one of the seventeen exactly as they do today — conforming on one model
// must never narrow another.
func TestMessageToolsIsInertUnlessTheRuleIsNamed(t *testing.T) {
	for id, body := range refusedBodies {
		t.Run(id, func(t *testing.T) {
			if _, _, ok := checkPolicy(nil, chatRoute, []byte(body)); !ok {
				t.Fatal("an endpoint that sold no terms must forward what it was given")
			}
			if _, _, ok := checkPolicy(kimiPolicy(), chatRoute, []byte(body)); !ok {
				t.Fatal("a policy that does not name this rule must not enforce it")
			}
		})
	}
}

// A shape the rule cannot positively read belongs to the upstream. Some of
// these are already broken, and the provider will say so in its own words; the
// rest are ordinary requests that a rule prepared to guess would break.
func TestMessageToolsLeavesUnreadableShapesToTheUpstream(t *testing.T) {
	const good = `{"type":"function","function":{"name":"get_weather","parameters":{"type":"object"}}}`

	for _, tc := range []struct {
		name string
		body string
	}{
		{"no messages at all", `{"model":"m"}`},
		{"messages is a string", `{"model":"m","messages":"hi"}`},
		{"messages is an object", `{"model":"m","messages":{"role":"user"}}`},
		{"a message that is null", `{"model":"m","messages":[null]}`},
		{"a message that is a string", `{"model":"m","messages":["hi"]}`},

		// An explicit null is how a good many clients serialise an absent
		// optional. Reading it as a broken declaration would refuse requests
		// that carry no tools and never meant to.
		{"tools sent as null on a user message", `{"model":"m","messages":[{"role":"user","content":"hi","tools":null}]}`},
		{"tools sent as null on a system message", `{"model":"m","messages":[{"role":"system","content":"x","tools":null}]}`},

		// Without a readable role there is no way to tell a declaration that is
		// in the wrong place from one that is in the right one.
		{"a message with no role", `{"model":"m","messages":[{"content":"","tools":[{"type":"bogus"}]}]}`},
		{"a role that is not a string", `{"model":"m","messages":[{"role":42,"content":"","tools":[{"type":"bogus"}]}]}`},

		// Only a string content was measured. A system message that omits it,
		// or sends parts, is a shape this rule has no standing to call empty
		// or non-empty.
		{"a system message with no content", `{"model":"m","messages":[{"role":"system","tools":[` + good + `]}]}`},
		{"content sent as parts", `{"model":"m","messages":[{"role":"system","content":[{"type":"text","text":"x"}],"tools":[` + good + `]}]}`},

		// The request-level array is read for names and never judged. A broken
		// one contributes nothing and refuses nothing.
		{"a request-level tools that is not an array", `{"model":"m","tools":"nonsense","messages":[{"role":"system","content":"","tools":[` + good + `]}]}`},
		{"a request-level tools with an unreadable item", `{"model":"m","tools":[null],"messages":[{"role":"system","content":"","tools":[` + good + `]}]}`},
		{"a request-level tools that repeats itself", `{"model":"m","tools":[{"type":"function","function":{"name":"dup"}},{"type":"function","function":{"name":"dup"}}]}`},

		// The ordinary request, which is nearly all of them.
		{"no declaration anywhere", `{"model":"m","messages":[{"role":"user","content":"hi"}],"tools":[` + good + `]}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, message, ok := checkPolicy(dynamicToolsPolicy(), chatRoute, []byte(tc.body)); !ok {
				t.Fatalf("must be left to the upstream, was refused with: %s", message)
			}
		})
	}
}

// Both of the board's wrong-role rows carry non-empty content as well, so
// neither of them can tell the role rule from the content rule — take the role
// check out and they are still refused, for the wrong reason. These bodies can
// tell: the misplaced declaration is the only thing wrong with them.
func TestOnlyASystemMessageMayDeclareTools(t *testing.T) {
	const tool = `{"type":"function","function":{"name":"get_weather"}}`

	// `developer` is here on a decision rather than a measurement. It was never
	// put to the provider, and it is refused because the alternative is a 200
	// carrying a tool that does not exist, which is the answer this rule was
	// written to stop giving.
	for _, role := range []string{"user", "assistant", "tool", "developer"} {
		t.Run(role, func(t *testing.T) {
			body := `{"model":"m","messages":[{"role":"` + role + `","content":"","tools":[` + tool + `]}]}`
			_, message, ok := checkPolicy(dynamicToolsPolicy(), chatRoute, []byte(body))
			if ok {
				t.Fatal("a declaration outside a system message must be refused")
			}
			if !strings.Contains(message, "has role") {
				t.Fatalf("the refusal must be about the role: %q", message)
			}
		})
	}

	// And the content rule bites only when there is a declaration beside it. A
	// system message that says something and declares nothing is every system
	// message anyone has ever sent.
	body := `{"model":"m","messages":[{"role":"system","content":"You are a helpful assistant."},{"role":"user","content":"hi"}]}`
	if _, message, ok := checkPolicy(dynamicToolsPolicy(), chatRoute, []byte(body)); !ok {
		t.Fatalf("a system message with no tools may say what it likes: %s", message)
	}
}

// The name rules, on either side of the line. The board measured four refusals
// and six accepted names; the rest of this table is the class each measured
// cell stands for, written down so that the widening is a decision somebody
// made rather than one that happened.
func TestToolNameRules(t *testing.T) {
	for _, tc := range []struct {
		label   string
		name    string
		refused bool
	}{
		// Measured accepted, as the names of the tools the board calls.
		{"an ordinary name", "get_weather", false},
		{"a single letter", "x", false},
		// Written out rather than taken from the constant. 256 is a fact about
		// the provider, and a test that read the bound off the code under test
		// would keep passing while somebody narrowed it to OpenAI's 64.
		{"exactly the limit", strings.Repeat("a", 256), false},

		// Measured refused.
		{"empty", "", true},
		{"a leading digit", "1bad_name", true},
		{"an at sign", "bad@name", true},
		{"one past the limit", strings.Repeat("a", 257), true},

		// The class the at sign stands for: every character outside
		// [A-Za-z0-9_-].
		{"a space", "bad name", true},
		{"a dot", "bad.name", true},
		{"a slash", "bad/name", true},
		{"a letter outside ASCII", "naivé", true},

		// And what stays inside it. A digit is refused first and nowhere else,
		// and the hyphen is here on the OpenAI charset this endpoint mirrors —
		// dropping it would refuse most of the hyphenated names in circulation.
		{"a hyphen", "get-weather", false},
		{"a trailing digit", "get_weather2", false},
		{"a leading underscore", "_private", false},
		{"mixed case", "MixedCase", false},
	} {
		t.Run(tc.label, func(t *testing.T) {
			body := `{"model":"m","messages":[{"role":"system","content":"","tools":[` +
				`{"type":"function","function":{"name":` + jsonQuoted(tc.name) + `}}]}]}`
			_, message, ok := checkPolicy(dynamicToolsPolicy(), chatRoute, []byte(body))
			if ok == tc.refused {
				t.Fatalf("refused = %v, want %v (%s)", !ok, tc.refused, message)
			}
		})
	}
}

// `parameters` is not a required field. The vendor's own missing-field matrix
// covers type, function and name and stops there, and a tool that takes no
// arguments is a tool.
func TestParametersAreNotRequired(t *testing.T) {
	body := `{"model":"m","messages":[{"role":"system","content":"","tools":[{"type":"function","function":{"name":"now"}}]}]}`
	if _, message, ok := checkPolicy(dynamicToolsPolicy(), chatRoute, []byte(body)); !ok {
		t.Fatalf("a tool with no parameters must be served: %s", message)
	}
}

// The diagnosis has to be worth the refusal. A customer who gets a 400 needs to
// know which declaration is wrong and what about it.
func TestDiagnosisNamesTheDeclarationAndTheFault(t *testing.T) {
	for _, tc := range []struct {
		id       string
		fragment string
	}{
		{"dynamic_tools.user_message_rejected", `messages[0] has role "user"`},
		{"dynamic_tools.assistant_message_rejected", `messages[1] has role "assistant"`},
		{"dynamic_tools.nonempty_content_rejected", "content empty"},
		{"dynamic_tools.missing_type_rejected", "messages[0].tools[0] has no 'type'."},
		{"dynamic_tools.missing_function_rejected", "messages[0].tools[0] has no 'function'."},
		{"dynamic_tools.missing_name_rejected", "messages[0].tools[0] has no 'function.name'."},
		{"dynamic_tools.invalid_name_empty_rejected", "empty 'function.name'"},
		{"dynamic_tools.invalid_name_too_long_257_rejected", "257 characters"},
		{"dynamic_tools.bogus_type_rejected", "is not a function tool"},
		{"dynamic_tools.mixed_valid_bogus_rejected", "messages[0].tools[1] is not a function tool"},
		{"dynamic_tools.not_array_rejected", "must be an array"},
		{"dynamic_tools.item_not_object_rejected", "messages[0].tools[0] is not an object"},
		{"dynamic_tools.duplicate_in_message_rejected", `messages[0].tools[1] declares the tool name "dup"`},
		{"dynamic_tools.duplicate_across_messages_rejected", `messages[1].tools[0] declares the tool name "dup"`},
		{"dynamic_tools.duplicate_global_dynamic_rejected", `messages[0].tools[0] declares the tool name "dup"`},
	} {
		t.Run(tc.id, func(t *testing.T) {
			_, message, ok := checkPolicy(dynamicToolsPolicy(), chatRoute, []byte(refusedBodies[tc.id]))
			if ok {
				t.Fatal("expected a refusal")
			}
			if !strings.Contains(message, tc.fragment) {
				t.Fatalf("message = %q, want it to carry %q", message, tc.fragment)
			}
		})
	}
}

// End to end, because a registry nothing consults is a registry that does not
// exist: the refusal has to reach the wire as a 400 in the OpenAI envelope,
// carry the diagnosis rather than the generic sentence, and leave an endpoint
// that sold no terms serving the same body.
func TestMalformedDeclarationIsRefusedOnTheWire(t *testing.T) {
	server, _ := upstreamServer(t)

	conforming := routeTo(t, server.URL, nil)
	conforming.Policy = dynamicToolsPolicy()
	open := routeTo(t, server.URL, nil)

	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"conforming": conforming,
		"open":       open,
	}}})

	malformed := refusedBodies["dynamic_tools.bogus_type_rejected"]

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(withModelNamed("conforming", malformed))))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}

	var envelope struct {
		Error struct {
			Message string `json:"message"`
			Type    string `json:"type"`
			Code    string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("error body is not the OpenAI envelope: %v", err)
	}
	if envelope.Error.Code != "invalid_parameter" || envelope.Error.Type != "invalid_request_error" {
		t.Fatalf("envelope = %+v", envelope.Error)
	}
	if !strings.Contains(envelope.Error.Message, "messages[0].tools[0]") {
		t.Fatalf("message = %q", envelope.Error.Message)
	}

	// The same body on an endpoint that sold nothing still goes upstream.
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(withModelNamed("open", malformed))))
	if rec.Code != http.StatusOK {
		t.Fatalf("open endpoint: status = %d, body = %s", rec.Code, rec.Body.String())
	}

	// And so does a declaration the provider serves.
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(withModelNamed("conforming", servedBodies["dynamic_tools.coexist_with_global"]))))
	if rec.Code != http.StatusOK {
		t.Fatalf("served declaration: status = %d, body = %s", rec.Code, rec.Body.String())
	}
}

// This gateway fronts the Anthropic Messages protocol as well as the OpenAI one,
// and a policy is attached to an endpoint, not to a wire format — so an
// Anthropic body reaches a rule written against OpenAI's shape. It must pass
// through untouched.
//
// The distinction that saves it is that `messages[].tools` is an OpenAI-only
// concept: Anthropic declares tools once at the top level and carries their use
// as `tool_use` / `tool_result` content blocks, so no message ever holds a
// `tools` key and the rule never engages. That is a real property of the
// protocol rather than a lucky accident, but it is worth a test, because the
// cost of being wrong is refusing every Claude Code request against this
// endpoint — and a stripper that assumed one protocol's frame shape took this
// service down once already.
func TestAnthropicBodiesDoNotTripTheOpenAIShapedRule(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{{Predicate: "messages.tools", Deny: true}}}

	for _, body := range []string{
		// Claude Code's own shape: tools declared once, at the top level.
		`{"model":"moonshotai/kimi-k3","max_tokens":1024,"system":"You are a helpful assistant.",
		  "tools":[{"name":"Bash","description":"run a command",
		            "input_schema":{"type":"object","properties":{"command":{"type":"string"}}}}],
		  "messages":[{"role":"user","content":"list the files"}]}`,

		// A tool call and its result, as content blocks.
		`{"model":"moonshotai/kimi-k3","max_tokens":1024,
		  "messages":[{"role":"user","content":[{"type":"text","text":"list the files"}]},
		              {"role":"assistant","content":[{"type":"tool_use","id":"tu_1","name":"Bash",
		                                              "input":{"command":"ls"}}]},
		              {"role":"user","content":[{"type":"tool_result","tool_use_id":"tu_1",
		                                         "content":"a.go b.go"}]}]}`,

		// Streaming, which is the shape that actually reaches production.
		`{"model":"moonshotai/kimi-k3","max_tokens":1024,"stream":true,
		  "messages":[{"role":"user","content":"hi"}]}`,
	} {
		if _, _, ok := checkPolicy(policy, chatRoute, []byte(body)); !ok {
			t.Fatalf("an Anthropic body was refused by an OpenAI-shaped rule:\n%s", body)
		}
	}
}

// `chat_template_kwargs` is a vLLM concept and not a provider one. Moonshot has
// no such field, ignores it, and thinks anyway — so honouring it here answered
// 200 to a request the provider answers 400 to, and turned thinking off on an
// endpoint sold as consistent with one that leaves it on.
//
// The field is taken out of the body by a `drop` term rather than read here, so
// this test is about the reading half: whatever the caller spells there, it does
// not move this rule.
func TestTheTemplateThinkingKwargIsNotReadAsThinkingOff(t *testing.T) {
	named := `{"type":"function","function":{"name":"f"}}`

	for _, kwargs := range []string{
		`{"thinking":false}`,
		`{"thinking":true}`,
		`{"thinking":"no"}`,
		`[]`,
		`null`,
	} {
		body := []byte(`{"model":"m","tool_choice":` + named + `,"chat_template_kwargs":` + kwargs + `}`)
		if _, _, ok := checkPolicy(kimiPolicy(), chatRoute, body); ok {
			t.Errorf("chat_template_kwargs %s moved a rule it must not reach", kwargs)
		}
	}

	// And the spelling the provider does read still works.
	body := []byte(`{"model":"m","tool_choice":` + named + `,"thinking":{"type":"disabled"},"chat_template_kwargs":{"thinking":true}}`)
	if _, _, ok := checkPolicy(kimiPolicy(), chatRoute, body); !ok {
		t.Error("thinking.type disabled must still be read, whatever the template kwarg says")
	}
}

// The drop term the rule above assumes, enforced end to end: the field a
// provider ignores must not reach an engine that does not.
func TestTheTemplateThinkingKwargIsTakenOutOfTheBody(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "chat_template_kwargs.thinking", Drop: true},
	}}

	body := []byte(`{"model":"m","messages":[],"chat_template_kwargs":{"thinking":false,"custom":1}}`)
	rewritten, err := rewriteBody(policy, chatRoute, body)
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if strings.Contains(string(rewritten), `"thinking"`) {
		t.Errorf("the kwarg reached the engine: %s", rewritten)
	}
	// Only that key. The object is the caller's and may carry kwargs this
	// endpoint has no opinion about.
	if !strings.Contains(string(rewritten), `"custom":1`) {
		t.Errorf("the drop took a sibling with it: %s", rewritten)
	}
}

// allowedToolsPolicy is the pair of drops that answers `tool_choice:
// {"type":"allowed_tools",...}` the way Moonshot's own frontend does.
//
// Measured against api.moonshot.ai, model kimi-k3, on 2026-09-10: the field is
// accepted and ignored. `prompt_tokens` is 202 whether it is sent or not, and
// `{"mode":"required","tools":[get_time]}` on a weather question still calls
// `get_weather`. So the vendor neither filters the tool list nor forces the
// subset, and the conforming answer is to take the field out — Dynamo's
// frontend refuses to deserialize it at all, which is a 400 on a request the
// vendor serves.
//
// Two rules because there are two spellings in the wild and they nest
// differently: OpenAI's own is `tool_choice.allowed_tools.{mode,tools}`, and the
// flat one the Kimi guidance suite sends is `tool_choice.{mode,tools}`.
func allowedToolsPolicy() *plane.Policy {
	return &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "tool_choice", Drop: true, WhenPresent: "tool_choice.allowed_tools"},
		{Route: chatRoute, Key: "tool_choice", Drop: true, WhenPresent: "tool_choice.mode"},
	}}
}

func TestAllowedToolsIsTakenOutOfTheBodyAndNothingElseIs(t *testing.T) {
	for _, c := range []struct {
		name       string
		toolChoice string
		dropped    bool
	}{
		{"the flat spelling the guidance suite sends",
			`{"type":"allowed_tools","mode":"auto","tools":[{"type":"function","name":"get_weather"}]}`, true},
		{"OpenAI's nested spelling",
			`{"type":"allowed_tools","allowed_tools":{"mode":"auto","tools":[{"type":"function","name":"get_weather"}]}}`, true},
		// The scalars are served. A rule that reached them would refuse
		// three modes the vendor answers, which is the whole risk of keying
		// the drop on a sub-path rather than on the key.
		{"auto", `"auto"`, false},
		{"required", `"required"`, false},
		{"none", `"none"`, false},
		// Named function has `function`, not `mode`, so it stays whole and the
		// existing named_while_thinking deny is still the thing that governs it.
		{"a named function", `{"type":"function","function":{"name":"get_weather"}}`, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			body := []byte(`{"model":"m","messages":[],"tools":[{"type":"function"}],"tool_choice":` + c.toolChoice + `}`)
			rewritten, err := rewriteBody(allowedToolsPolicy(), chatRoute, body)
			if err != nil {
				t.Fatalf("rewrite: %v", err)
			}
			gone := !strings.Contains(string(rewritten), `"tool_choice"`)
			if gone != c.dropped {
				t.Errorf("tool_choice dropped=%v, want %v: %s", gone, c.dropped, rewritten)
			}
			// Whatever happened to tool_choice, the tools the caller declared
			// are the ones the model must still be offered.
			if !strings.Contains(string(rewritten), `"tools"`) {
				t.Errorf("the drop took the tools array with it: %s", rewritten)
			}
		})
	}
}

// The provider's temperature pin, both modes, as measured on 2026-09-11.
//
// Two cells are accepted and every other one is a 400, which is why this is a
// predicate rather than an `enum` on the key: the accepted value depends on
// another field of the same request, and `when_present` cannot ask the question
// because thinking is on by the *absence* of `thinking`.
func TestTemperatureIsPinnedPerThinkingMode(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Predicate: "temperature.not_pinned_for_mode", Deny: true},
	}}

	for _, probe := range []struct {
		thinking string
		value    string
		accepted bool
	}{
		// Thinking on, spelled and by omission. Only 1 is served.
		{`,"thinking":{"type":"enabled"}`, "1", true},
		{`,"thinking":{"type":"enabled"}`, "1.0", true},
		{`,"thinking":{"type":"enabled"}`, "0.6", false},
		{`,"thinking":{"type":"enabled"}`, "0", false},
		{"", "1", true},
		{"", "0.6", false},
		{"", "0", false},

		// Thinking off. Only 0.6 is served — and 1.0, which is the accepted
		// value one line up, is refused here. This is the half the report that
		// prompted the rule called "fine on both".
		{`,"thinking":{"type":"disabled"}`, "0.6", true},
		{`,"thinking":{"type":"disabled"}`, "1", false},
		{`,"thinking":{"type":"disabled"}`, "0", false},
	} {
		body := []byte(`{"model":"m","messages":[],"temperature":` + probe.value + probe.thinking + `}`)
		_, _, ok := checkPolicy(policy, chatRoute, body)
		if ok != probe.accepted {
			t.Errorf("temperature %s%s: accepted=%v, want %v", probe.value, probe.thinking, ok, probe.accepted)
		}
	}
}

// An absent temperature is not a violation — the minimal request every client
// starts with must still be served — and a malformed one belongs to the
// upstream, which can say what is wrong with it in its own words.
func TestTheTemperaturePinDemandsNothingAndDiagnosesNothingItCannotRead(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Predicate: "temperature.not_pinned_for_mode", Deny: true},
	}}

	for _, body := range []string{
		`{"model":"m","messages":[]}`,
		`{"model":"m","messages":[],"temperature":null}`,
		`{"model":"m","messages":[],"temperature":"hot"}`,
		`{"model":"m","messages":[],"temperature":1,"thinking":"broken"}`,
	} {
		if _, _, ok := checkPolicy(policy, chatRoute, []byte(body)); !ok {
			t.Errorf("refused a request this rule has no standing to refuse:\n%s", body)
		}
	}
}

// The refusal names the value that would have worked, and which mode decided
// it. A caller who sent 0.6 with thinking on is one field away from a served
// request and must be able to see which field.
func TestTheTemperaturePinSaysWhichValueTheModeAllows(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Predicate: "temperature.not_pinned_for_mode", Deny: true},
	}}

	for _, probe := range []struct{ body, code, message string }{
		{
			`{"model":"m","messages":[],"temperature":0.6,"thinking":{"type":"enabled"}}`,
			"invalid_parameter",
			"The only accepted value for 'temperature' is 1 while thinking is enabled.",
		},
		{
			`{"model":"m","messages":[],"temperature":1,"thinking":{"type":"disabled"}}`,
			"invalid_parameter",
			"The only accepted value for 'temperature' is 0.6 while thinking is disabled.",
		},
	} {
		code, message, ok := checkPolicy(policy, chatRoute, []byte(probe.body))
		if ok {
			t.Fatalf("served a temperature the provider refuses:\n%s", probe.body)
		}
		if code != probe.code || message != probe.message {
			t.Errorf("got %s %q, want %s %q", code, message, probe.code, probe.message)
		}
	}
}

// Bound 5 of docs/decisions/0017: the field that reconfigures the engine's media
// loader is the caller's one way to unwind every decode bound the recipe and the
// worker hold, so it does not reach an upstream and it does not depend on a
// policy being attached.
func TestMediaDecodeKwargsNeverReachTheEngine(t *testing.T) {
	for _, c := range []struct {
		name    string
		body    string
		kept    []string
		dropped bool
	}{
		{
			name:    "at the top level",
			body:    `{"model":"m","messages":[],"media_io_kwargs":{"video":{"num_frames":-1}}}`,
			kept:    []string{`"model"`, `"messages"`},
			dropped: true,
		},
		{
			name:    "hand-written inside extra_body",
			body:    `{"model":"m","messages":[],"extra_body":{"media_io_kwargs":{"video":{"num_frames":-1}},"custom":1}}`,
			kept:    []string{`"extra_body"`, `"custom":1`},
			dropped: true,
		},
		{
			name:    "in both places at once",
			body:    `{"model":"m","media_io_kwargs":{},"extra_body":{"media_io_kwargs":{}}}`,
			kept:    []string{`"extra_body"`},
			dropped: true,
		},
		{
			name:    "an extra_body this hop cannot read is left to the upstream",
			body:    `{"model":"m","extra_body":"media_io_kwargs"}`,
			kept:    []string{`"extra_body":"media_io_kwargs"`},
			dropped: false,
		},
		{
			name:    "a request that never asked for it",
			body:    `{"model":"m","messages":[{"role":"user","content":"hi"}]}`,
			kept:    []string{`"messages"`},
			dropped: false,
		},
	} {
		t.Run(c.name, func(t *testing.T) {
			rewritten, err := rewriteBody(nil, chatRoute, []byte(c.body))
			if err != nil {
				t.Fatalf("rewrite: %v", err)
			}
			carried := strings.Contains(string(c.body), `"media_io_kwargs":`)
			if gone := carried && !strings.Contains(string(rewritten), `"media_io_kwargs":`); gone != c.dropped {
				t.Errorf("media_io_kwargs dropped=%v, want %v: %s", gone, c.dropped, rewritten)
			}
			if !c.dropped && string(rewritten) != c.body {
				t.Errorf("a request with nothing to strip was re-encoded: %s", rewritten)
			}
			for _, kept := range c.kept {
				if !strings.Contains(string(rewritten), kept) {
					t.Errorf("the strip took %s with it: %s", kept, rewritten)
				}
			}
		})
	}
}

// The strip is silent. A tuning hint on an otherwise valid request is not a
// reason to fail the request, and a client whose own defaults spell out what we
// already do must not see a 400 for agreeing with us.
func TestMediaDecodeKwargsAreDroppedAndNotRefused(t *testing.T) {
	body := []byte(`{"model":"m","messages":[],"media_io_kwargs":{"video":{"num_frames":-1}}}`)
	if _, _, ok := checkPolicy(kimiPolicy(), chatRoute, body); !ok {
		t.Fatal("the field must be dropped, never refused")
	}
}

// The strip runs beside the endpoint's own terms and neither one costs the
// other: a policy still writes what it declares, and a request carrying nothing
// to strip is still forwarded as the exact bytes that arrived.
func TestMediaDecodeKwargsStripCoexistsWithPolicyTerms(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "chat_template_kwargs.thinking", Drop: true},
	}}

	body := []byte(`{"model":"m","media_io_kwargs":{},"chat_template_kwargs":{"thinking":false}}`)
	rewritten, err := rewriteBody(policy, chatRoute, body)
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if strings.Contains(string(rewritten), `"media_io_kwargs"`) ||
		strings.Contains(string(rewritten), `"thinking"`) {
		t.Errorf("both terms must apply in one pass: %s", rewritten)
	}

	untouched := []byte(`{"model":"m","messages":[]}`)
	forwarded, err := rewriteBody(nil, chatRoute, untouched)
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if &forwarded[0] != &untouched[0] {
		t.Errorf("a request with nothing to write was re-encoded: %s", forwarded)
	}
}

// The field is a chat-completions surface. The other protocols this hop fronts
// carry their own bodies and nobody has measured this key on them, so it is left
// exactly where it was found.
func TestMediaDecodeKwargsStripIsScopedToChatCompletions(t *testing.T) {
	for _, route := range []string{embeddingsRoute, messagesRoute, responsesRoute} {
		t.Run(route, func(t *testing.T) {
			body := []byte(`{"model":"m","media_io_kwargs":{}}`)
			rewritten, err := rewriteBody(nil, route, body)
			if err != nil {
				t.Fatalf("rewrite: %v", err)
			}
			if !strings.Contains(string(rewritten), `"media_io_kwargs"`) {
				t.Errorf("stripped off the route it was measured on: %s", rewritten)
			}
		})
	}
}
