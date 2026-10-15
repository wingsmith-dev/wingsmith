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

// The defect this scope was added for. An endpoint answers on every path the
// gateway fronts, and a policy is attached to the endpoint — so before a rule
// could name a route, a temperature pin measured against the provider's chat
// completions was enforced against embeddings, Anthropic messages and the
// responses API as well, none of which anybody had measured.
func TestARouteScopedRuleIsInertOnEveryOtherPath(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "temperature", Enum: []any{float64(1)}},
	}}
	body := []byte(`{"model":"m","temperature":0.7}`)

	if _, _, ok := checkPolicy(policy, chatRoute, body); ok {
		t.Fatal("the route it was measured on must still refuse it")
	}
	for _, route := range []string{embeddingsRoute, messagesRoute, responsesRoute, "/v1/completions"} {
		if _, message, ok := checkPolicy(policy, route, body); !ok {
			t.Fatalf("%s: a term sold about chat completions reached it: %s", route, message)
		}
	}
}

// An omitted route is not a narrower scope, it is the absence of one. That is
// what every policy written before scopes existed means, and what a migrated one
// still means.
func TestAnUnscopedRuleMatchesEveryPath(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Key: "temperature", Enum: []any{float64(1)}},
	}}
	for _, route := range []string{chatRoute, embeddingsRoute, messagesRoute, responsesRoute} {
		if _, _, ok := checkPolicy(policy, route, []byte(`{"model":"m","temperature":0.7}`)); ok {
			t.Fatalf("%s: an unscoped rule must apply here", route)
		}
	}
}

func TestStreamScope(t *testing.T) {
	streaming := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Stream: boolean(true), Key: "n", Enum: []any{float64(1)}},
	}}

	for _, tc := range []struct {
		name    string
		body    string
		refused bool
	}{
		{"streaming, and the rule is about streaming", `{"stream":true,"n":2}`, true},
		{"not streaming", `{"stream":false,"n":2}`, false},

		// Absent is a positive answer and it is false: `stream` defaults to off
		// on every wire this gateway fronts.
		{"stream left out", `{"n":2}`, false},

		// A mode this hop cannot name is not a mode a scoped rule applies to.
		// The upstream will answer for the field.
		{"stream is not a boolean", `{"stream":"yes","n":2}`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, _, ok := checkPolicy(streaming, chatRoute, []byte(tc.body))
			if ok == tc.refused {
				t.Fatalf("refused = %v, want %v", !ok, tc.refused)
			}
		})
	}

	// A nil stream is both modes, which is what a rule that says nothing about
	// the transport has to mean.
	both := &plane.Policy{Version: 2, Rules: []plane.Rule{{Key: "n", Enum: []any{float64(1)}}}}
	for _, body := range []string{`{"stream":true,"n":2}`, `{"stream":false,"n":2}`, `{"n":2}`} {
		if _, _, ok := checkPolicy(both, chatRoute, []byte(body)); ok {
			t.Fatalf("%s: a rule with no stream applies to both modes", body)
		}
	}
}

// A key is a JSON path, which is the whole reason `thinking.type` is now
// writable as a rule: v1 could only name a top-level field, so the one term the
// K3 endpoint most needed could not be said at all.
func TestDottedPathResolution(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Key: "thinking.type", Enum: []any{"enabled", "disabled", "adaptive"}},
	}}

	for _, tc := range []struct {
		name    string
		body    string
		refused bool
	}{
		{"a value the enum names", `{"thinking":{"type":"adaptive"}}`, false},
		{"a value it does not", `{"thinking":{"type":"sometimes"}}`, true},

		// Absent at any depth is never a violation, for the same reason an
		// absent top-level parameter never was: the provider refuses a bad
		// value, it does not demand the field.
		{"no thinking at all", `{"model":"m"}`, false},
		{"a thinking with no type", `{"thinking":{"keep":"all"}}`, false},

		// A parent this rule cannot read is not one it can judge. Each of these
		// is either already broken, and the upstream will say so in its own
		// words, or a shape the rule was never measured against.
		{"thinking sent as a string", `{"thinking":"enabled"}`, false},
		{"thinking sent as null", `{"thinking":null}`, false},
		{"thinking sent as an array", `{"thinking":[]}`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, _, ok := checkPolicy(policy, chatRoute, []byte(tc.body))
			if ok == tc.refused {
				t.Fatalf("refused = %v, want %v", !ok, tc.refused)
			}
		})
	}

	// Depth is not limited to two, and the message names the path the author
	// wrote rather than the leaf.
	deep := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Key: "a.b.c", Enum: []any{float64(1)}},
	}}
	_, message, ok := checkPolicy(deep, chatRoute, []byte(`{"a":{"b":{"c":2}}}`))
	if ok {
		t.Fatal("a three-segment path must resolve")
	}
	if !strings.Contains(message, "'a.b.c'") {
		t.Fatalf("message = %q, want it to name the path", message)
	}
}

// The concrete conformance failure this language was extended for. K3 answers
// 400 to `{"thinking":{"keep":"all"}}` with "thinking.type must be enabled,
// disabled, or adaptive", and the provider's contract is that an omitted type means
// enabled — so the gateway spells it out on the way through.
func TestDefaultFillsOnlyWhenTheParentIsPresent(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "thinking.type", Default: "enabled"},
	}}

	for _, tc := range []struct {
		name string
		body string
		want string
	}{
		{
			"a thinking object with no type",
			`{"model":"m","thinking":{"keep":"all"}}`,
			`{"model":"m","thinking":{"keep":"all","type":"enabled"}}`,
		},
		{
			"an empty thinking object is still a parent",
			`{"model":"m","thinking":{}}`,
			`{"model":"m","thinking":{"type":"enabled"}}`,
		},

		// The rule that matters most. Injecting `thinking` into a request that
		// never mentioned it would turn every plain call on this endpoint into
		// one that asked for thinking, which is a change to what every caller
		// gets — made on their behalf, by a rule that was only supposed to write
		// down what they already meant.
		{
			"no thinking at all",
			`{"model":"m","messages":[]}`,
			`{"model":"m","messages":[]}`,
		},

		// A caller who wrote something there wrote it on purpose. A default is
		// for what they left out.
		{
			"a type the caller sent",
			`{"model":"m","thinking":{"type":"disabled"}}`,
			`{"model":"m","thinking":{"type":"disabled"}}`,
		},
		{
			"a type the caller sent as null",
			`{"model":"m","thinking":{"type":null}}`,
			`{"model":"m","thinking":{"type":null}}`,
		},

		// A parent that is not an object is not a parent.
		{
			"thinking sent as a string",
			`{"model":"m","thinking":"enabled"}`,
			`{"model":"m","thinking":"enabled"}`,
		},
		{
			"thinking sent as null",
			`{"model":"m","thinking":null}`,
			`{"model":"m","thinking":null}`,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := rewriteBody(policy, chatRoute, []byte(tc.body))
			if err != nil {
				t.Fatalf("rewrite: %v", err)
			}
			assertSameJSON(t, got, tc.want)
		})
	}

	// And it is scoped like every other rule.
	got, err := rewriteBody(policy, messagesRoute, []byte(`{"model":"m","thinking":{"keep":"all"}}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if strings.Contains(string(got), "enabled") {
		t.Fatalf("body = %s, want a chat-completions default left off another path", got)
	}
}

// A top-level key's parent is the root, which is always there.
func TestATopLevelDefaultAlwaysHasItsParent(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{{Key: "n", Default: float64(1)}}}

	got, err := rewriteBody(policy, chatRoute, []byte(`{"model":"m"}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	assertSameJSON(t, got, `{"model":"m","n":1}`)
}

// Nothing on the path is ever created, at any depth.
func TestADefaultNeverCreatesAnIntermediateObject(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{{Key: "a.b.c", Default: float64(1)}}}

	for _, tc := range []struct{ body, want string }{
		{`{"a":{"b":{}}}`, `{"a":{"b":{"c":1}}}`},
		{`{"a":{}}`, `{"a":{}}`},
		{`{}`, `{}`},
		{`{"a":{"b":"not an object"}}`, `{"a":{"b":"not an object"}}`},
	} {
		got, err := rewriteBody(policy, chatRoute, []byte(tc.body))
		if err != nil {
			t.Fatalf("rewrite: %v", err)
		}
		assertSameJSON(t, got, tc.want)
	}
}

// Two defaults under one parent both land, and the second reads the object the
// first already wrote into rather than the bytes that arrived.
func TestTwoDefaultsUnderOneParent(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Key: "thinking.type", Default: "enabled"},
		{Key: "thinking.keep", Default: "all"},
	}}

	got, err := rewriteBody(policy, chatRoute, []byte(`{"thinking":{"effort":"max"}}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	assertSameJSON(t, got, `{"thinking":{"effort":"max","keep":"all","type":"enabled"}}`)
}

// The ordinary request is nearly every request, and it must cost nothing. A body
// with no default to fill is forwarded as the exact bytes that arrived — not
// re-encoded, which would reorder its keys for no reason at all.
func TestABodyWithNothingToFillIsNotRewritten(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Key: "thinking.type", Default: "enabled"},
		{Key: "temperature", Enum: []any{float64(1)}},
	}}

	for _, body := range []string{
		`{"model":"m","temperature":1,"messages":[{"role":"user","content":"hi"}]}`,
		`{"model":"m","thinking":{"type":"disabled"}}`,

		// Not JSON at all: a multipart upload reaches this line intact, and its
		// parameters are form fields rather than JSON values.
		"--boundary\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nm\r\n--boundary--\r\n",
	} {
		got, err := rewriteBody(policy, chatRoute, []byte(body))
		if err != nil {
			t.Fatalf("rewrite: %v", err)
		}
		if string(got) != body {
			t.Fatalf("body was rewritten:\n got %s\nwant %s", got, body)
		}
	}

	// And a nil policy never looks at the body at all.
	body := `{"model":"m","thinking":{}}`
	got, err := rewriteBody(nil, chatRoute, []byte(body))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if string(got) != body {
		t.Fatalf("body = %s, want it untouched by an endpoint that sold nothing", got)
	}
}

// Constraints are cumulative: every rule whose scope matches applies, and the
// request has to satisfy all of them. There is no most-specific-wins, so a rule
// that names a route does not displace one that does not.
func TestConstraintsAreCumulative(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Key: "temperature", Min: number(0), Max: number(2)},
		{Route: chatRoute, Key: "temperature", Enum: []any{float64(1), float64(2)}},
	}}

	for _, tc := range []struct {
		name    string
		route   string
		body    string
		refused bool
	}{
		{"satisfies both", chatRoute, `{"temperature":1}`, false},
		{"breaks the range only", chatRoute, `{"temperature":3}`, true},
		{"breaks the enum only", chatRoute, `{"temperature":0.5}`, true},
		{"breaks both", chatRoute, `{"temperature":5}`, true},

		// Off that route only the unscoped rule is in play, so a value the
		// narrower rule would have refused is served.
		{"the range alone, elsewhere", embeddingsRoute, `{"temperature":0.5}`, false},
		{"and it still bites", embeddingsRoute, `{"temperature":3}`, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, _, ok := checkPolicy(policy, tc.route, []byte(tc.body))
			if ok == tc.refused {
				t.Fatalf("refused = %v, want %v", !ok, tc.refused)
			}
		})
	}
}

// The order the rules were written in is not part of the language. A policy and
// a re-serialisation of it that reported different violations would be a policy
// nobody could store, so the evaluation order is derived from the rules
// themselves.
func TestTheReportedViolationDoesNotDependOnTheArrayOrder(t *testing.T) {
	forwards := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Key: "temperature", Enum: []any{float64(1)}},
		{Key: "top_p", Enum: []any{0.95}},
		{Predicate: "tool_choice.named_while_thinking", Deny: true},
	}}
	backwards := &plane.Policy{Version: 2, Rules: []plane.Rule{
		forwards.Rules[2], forwards.Rules[1], forwards.Rules[0],
	}}

	body := []byte(`{"temperature":0.7,"top_p":0.9,"tool_choice":{"type":"function","function":{"name":"f"}}}`)
	_, first, _ := checkPolicy(forwards, chatRoute, body)
	if _, message, _ := checkPolicy(backwards, chatRoute, body); message != first {
		t.Fatalf("the order of the array changed the answer: %q then %q", first, message)
	}
	for i := 0; i < 50; i++ {
		if _, message, _ := checkPolicy(forwards, chatRoute, body); message != first {
			t.Fatalf("violation is unstable: %q then %q", first, message)
		}
	}
}

// A key is a path into the body and a predicate is a name in a Go registry, and
// the two never fall back to one another. They share a spelling here on purpose:
// `tool_choice.named_while_thinking` is a registered predicate and is not a path
// anything sends, and `thinking.type` is a path and is not a registered rule.
func TestKeyAndPredicateAreDifferentNamespaces(t *testing.T) {
	// The predicate's name, used as a key. It resolves nothing, because no body
	// carries a `tool_choice` object with a `named_while_thinking` in it — so
	// the request the predicate refuses is served.
	asKey := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Key: "tool_choice.named_while_thinking", Deny: true},
	}}
	refused := []byte(`{"tool_choice":{"type":"function","function":{"name":"f"}}}`)
	if _, message, ok := checkPolicy(asKey, chatRoute, refused); !ok {
		t.Fatalf("a key must be read out of the body and never looked up as a rule: %s", message)
	}

	// A path, used as a predicate. It is not in either registry, so it is
	// ignored — reported at refresh time rather than enforced on a guess.
	asPredicate := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Predicate: "thinking.type", Deny: true},
	}}
	if _, _, ok := checkPolicy(asPredicate, chatRoute, []byte(`{"thinking":{"type":"enabled"}}`)); !ok {
		t.Fatal("a predicate this build does not implement must not refuse anything")
	}

	// And the predicate still does its own job when it is named as one, which is
	// the registry the v1 `deny` list pointed at and still points at.
	asPredicateProper := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Predicate: "tool_choice.named_while_thinking", Deny: true},
	}}
	code, _, ok := checkPolicy(asPredicateProper, chatRoute, refused)
	if ok {
		t.Fatal("the registered predicate must still refuse the cell the provider refuses")
	}
	if code != "unsupported_parameter" {
		t.Fatalf("code = %q", code)
	}
}

// A predicate that is named but not denied says nothing about anything. It is
// refused where the policy is written; here it is simply inert.
func TestAPredicateThatDoesNotDenyIsInert(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Predicate: "tool_choice.named_while_thinking"},
	}}
	body := []byte(`{"tool_choice":{"type":"function","function":{"name":"f"}}}`)
	if _, _, ok := checkPolicy(policy, chatRoute, body); !ok {
		t.Fatal("a rule that denies nothing must refuse nothing")
	}
}

// Deny on a key refuses the field whenever it is sent, which is how an endpoint
// says it does not take one at all.
func TestDenyOnAKeyRefusesThePresenceOfTheField(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "logit_bias", Deny: true},
	}}

	code, message, ok := checkPolicy(policy, chatRoute, []byte(`{"logit_bias":{"1":1}}`))
	if ok {
		t.Fatal("a denied key must be refused when it is sent")
	}
	if code != "unsupported_parameter" {
		t.Fatalf("code = %q", code)
	}
	if !strings.Contains(message, "'logit_bias'") {
		t.Fatalf("message = %q, want it to name the parameter", message)
	}

	// Absent is not sent, and an explicit null is. A client that serialises an
	// absent optional as null is asking for the field, and this rule is about
	// the field.
	if _, _, ok := checkPolicy(policy, chatRoute, []byte(`{"model":"m"}`)); !ok {
		t.Fatal("a request that does not send it must be served")
	}
	if _, _, ok := checkPolicy(policy, chatRoute, []byte(`{"logit_bias":null}`)); ok {
		t.Fatal("an explicit null is still the field being sent")
	}
}

// End to end. The default has to reach the engine, not just the test for the
// function that computes it, and the endpoint that sold nothing has to keep
// getting exactly what its caller wrote.
func TestDefaultsReachTheUpstream(t *testing.T) {
	server, _ := upstreamServer(t)

	conforming := routeTo(t, server.URL, nil)
	conforming.Policy = &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "thinking.type", Default: "enabled"},
	}}
	open := routeTo(t, server.URL, nil)

	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"conforming": conforming,
		"open":       open,
	}}})

	// The upstream echoes what it was sent, so the response body is the request
	// the engine saw.
	send := func(route, body string) (int, string) {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, route, strings.NewReader(body)))
		return rec.Code, rec.Body.String()
	}

	code, seen := send(chatRoute, `{"model":"conforming","thinking":{"keep":"all"}}`)
	if code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", code, seen)
	}
	assertSameJSON(t, []byte(seen), `{"model":"conforming","thinking":{"keep":"all","type":"enabled"}}`)

	// A request that never mentioned thinking arrives with nothing added.
	code, seen = send(chatRoute, `{"model":"conforming","messages":[]}`)
	if code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", code, seen)
	}
	if strings.Contains(seen, "thinking") {
		t.Fatalf("body = %s, want no thinking invented", seen)
	}

	// Off the route the rule names, nothing is written either.
	code, seen = send(messagesRoute, `{"model":"conforming","thinking":{"keep":"all"}}`)
	if code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", code, seen)
	}
	if strings.Contains(seen, "enabled") {
		t.Fatalf("body = %s, want a chat-completions default left off /v1/messages", seen)
	}

	// And an endpoint that sold no terms gets its body forwarded verbatim,
	// which is the property the whole per-endpoint design exists for.
	body := `{"model":"open","thinking":{"keep":"all"}}`
	code, seen = send(chatRoute, body)
	if code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", code, seen)
	}
	if seen != body {
		t.Fatalf("body = %s, want it forwarded untouched", seen)
	}
}

// A rule scoped to chat completions must not answer for a request to another
// path on the same endpoint, all the way out to the status code.
func TestARouteScopedRefusalDoesNotReachAnotherPathOnTheWire(t *testing.T) {
	server, _ := upstreamServer(t)

	route := routeTo(t, server.URL, nil)
	route.Policy = kimiPolicy()
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{"kimi": route}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, chatRoute,
		strings.NewReader(`{"model":"kimi","temperature":0.7}`)))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("chat completions: status = %d, body = %s", rec.Code, rec.Body.String())
	}

	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, embeddingsRoute,
		strings.NewReader(`{"model":"kimi","input":"hello","temperature":0.7}`)))
	if rec.Code != http.StatusOK {
		t.Fatalf("embeddings: status = %d, body = %s", rec.Code, rec.Body.String())
	}
}

// assertSameJSON compares two bodies as JSON values. A rewritten body is
// re-encoded from a map, so its key order is Go's and not the caller's — the
// bytes differ where the request does not.
func assertSameJSON(t *testing.T, got []byte, want string) {
	t.Helper()
	var left, right any
	if err := json.Unmarshal(got, &left); err != nil {
		t.Fatalf("got is not JSON: %v (%s)", err, got)
	}
	if err := json.Unmarshal([]byte(want), &right); err != nil {
		t.Fatalf("want is not JSON: %v", err)
	}
	normalised, _ := json.Marshal(left)
	expected, _ := json.Marshal(right)
	if string(normalised) != string(expected) {
		t.Fatalf("body:\n got %s\nwant %s", normalised, expected)
	}
}

// The second conformance failure this language was extended for, and the one
// that needed a term the shape did not have. K3's contract — their own
// `test_reasoning_effort_ignored_when_effort_present` — is that a request
// carrying both `thinking.effort` and the OpenAI-style `reasoning_effort`
// honours the first and silently ignores the second.
//
// Measured on our endpoint: `thinking.effort=max` alone produced 884 characters
// of reasoning trace, and `thinking.effort=low` beside `reasoning_effort=max`
// produced 889 — so the engine behind this hop was reading `reasoning_effort`
// where the provider ignores it, and the trace that should have been markedly
// shorter was not.
//
// Neither existing term reaches it. `deny` answers 400 to a request the vendor
// serves, and `default` is the opposite operation. `drop` with a `when_present`
// condition is the whole of it.
func TestReasoningEffortIsDroppedOnlyWhenThinkingEffortIsThere(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, WhenPresent: "thinking.effort", Key: "reasoning_effort", Drop: true},
	}}

	for _, tc := range []struct {
		name string
		body string
		want string
	}{
		{
			"both, so the vendor's own field wins and the other goes",
			`{"model":"m","thinking":{"effort":"low"},"reasoning_effort":"max"}`,
			`{"model":"m","thinking":{"effort":"low"}}`,
		},

		// Nothing to defer to, so nothing is taken away. This is the half that
		// makes it a condition rather than a blanket removal: `reasoning_effort`
		// on its own is a field the provider honours, and dropping it would
		// silently ignore what the caller asked for.
		{
			"reasoning_effort alone",
			`{"model":"m","reasoning_effort":"max"}`,
			`{"model":"m","reasoning_effort":"max"}`,
		},
		{
			"a thinking object with no effort in it",
			`{"model":"m","thinking":{"type":"enabled"},"reasoning_effort":"max"}`,
			`{"model":"m","thinking":{"type":"enabled"},"reasoning_effort":"max"}`,
		},
		{
			"thinking.effort alone",
			`{"model":"m","thinking":{"effort":"low"}}`,
			`{"model":"m","thinking":{"effort":"low"}}`,
		},
		{
			"neither",
			`{"model":"m","messages":[]}`,
			`{"model":"m","messages":[]}`,
		},

		// A condition this hop cannot read is not a condition it acts on, the
		// same way an unreadable parent is not a parent.
		{
			"thinking sent as a string",
			`{"model":"m","thinking":"low","reasoning_effort":"max"}`,
			`{"model":"m","thinking":"low","reasoning_effort":"max"}`,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := rewriteBody(policy, chatRoute, []byte(tc.body))
			if err != nil {
				t.Fatalf("rewrite: %v", err)
			}
			assertSameJSON(t, got, tc.want)
		})
	}

	// And it is scoped like everything else: `/v1/responses` has a
	// `reasoning.effort` of its own and was never measured here.
	got, err := rewriteBody(policy, responsesRoute,
		[]byte(`{"model":"m","thinking":{"effort":"low"},"reasoning_effort":"max"}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	assertSameJSON(t, got, `{"model":"m","thinking":{"effort":"low"},"reasoning_effort":"max"}`)
}

// What the drop above was measured against, and what it turned out not to be.
//
// Dropping `reasoning_effort` makes it *ignored*, which is one half of K3's
// contract. The other half is that `thinking.effort` is *honoured*, and the drop
// does nothing for it. Measured on a live endpoint, asking the model its
// own effective effort — the vendor suite's `_ask_effort` probe, which reads the
// resolved level off the system prompt instead of inferring it from the length
// of the trace — answers `max` for `thinking.effort=low` sent alone, with no
// `reasoning_effort` in the request for any rule to remove. Nothing behind this
// hop reads `thinking.effort`, so taking the loser away only leaves the engine on
// its own default, which is `max`. `effort=low` beside `reasoning_effort=max` and
// `effort=max` alone then both run at max, and the trace-length comparison that
// was standing in for the contract is a coin flip between two samples of one
// distribution — which is how it read as a pass.
//
// So the term is a copy, not a removal: carry the vendor's field onto the field
// the engine reads. It subsumes the drop — the caller's `reasoning_effort` is
// overwritten, so it is ignored — and it closes the half the drop could not.
func TestThinkingEffortIsCarriedOntoTheFieldTheEngineReads(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "reasoning_effort", ValueFrom: "thinking.effort"},
	}}

	for _, tc := range []struct {
		name string
		body string
		want string
	}{
		{
			"both, so the vendor's field wins by replacing the other",
			`{"model":"m","thinking":{"effort":"low"},"reasoning_effort":"max"}`,
			`{"model":"m","thinking":{"effort":"low"},"reasoning_effort":"low"}`,
		},

		// The half the drop never had. A caller who sent only the vendor's
		// spelling gets the engine's spelling written for them, which is the
		// only reason the level they asked for is the level that runs.
		{
			"thinking.effort alone is installed rather than left unread",
			`{"model":"m","thinking":{"type":"enabled","keep":"all","effort":"high"}}`,
			`{"model":"m","thinking":{"type":"enabled","keep":"all","effort":"high"},"reasoning_effort":"high"}`,
		},

		// Nothing to defer to, so nothing is written. `reasoning_effort` on its
		// own is a field the provider honours, and overwriting it would silently
		// ignore what the caller asked for — the same half of the condition the
		// drop got right.
		{
			"reasoning_effort alone",
			`{"model":"m","reasoning_effort":"max"}`,
			`{"model":"m","reasoning_effort":"max"}`,
		},
		{
			"a thinking object with no effort in it",
			`{"model":"m","thinking":{"type":"enabled"},"reasoning_effort":"max"}`,
			`{"model":"m","thinking":{"type":"enabled"},"reasoning_effort":"max"}`,
		},
		{
			"neither",
			`{"model":"m","messages":[]}`,
			`{"model":"m","messages":[]}`,
		},

		// A source this hop cannot read is not a source it copies, the same way
		// an unreadable parent is not a parent.
		{
			"thinking sent as a string",
			`{"model":"m","thinking":"low","reasoning_effort":"max"}`,
			`{"model":"m","thinking":"low","reasoning_effort":"max"}`,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := rewriteBody(policy, chatRoute, []byte(tc.body))
			if err != nil {
				t.Fatalf("rewrite: %v", err)
			}
			assertSameJSON(t, got, tc.want)
		})
	}

	// Scoped like everything else: `/v1/responses` has a `reasoning.effort` of
	// its own and was never measured here.
	got, err := rewriteBody(policy, responsesRoute,
		[]byte(`{"model":"m","thinking":{"effort":"low"},"reasoning_effort":"max"}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	assertSameJSON(t, got, `{"model":"m","thinking":{"effort":"low"},"reasoning_effort":"max"}`)
}

// A copy already carrying the value it would write leaves the request alone, so
// the exact bytes that arrived are what gets forwarded.
func TestACarryOfAValueAlreadyThereDoesNotRewriteTheBody(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "reasoning_effort", ValueFrom: "thinking.effort"},
	}}

	const body = `{"model":"m","thinking":{"effort":"low"},"reasoning_effort":"low"}`
	got, err := rewriteBody(policy, chatRoute, []byte(body))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if string(got) != body {
		t.Fatalf("body was rewritten:\n got %s\nwant %s", got, body)
	}
}

// Every source is read out of the request as it arrived, so a rule that carries a
// key another rule drops carries the value the caller sent — not whatever the
// other rule left behind, and not a different answer depending on which of them
// the evaluation order happened to put first.
func TestACarryReadsTheBodyAsItArrivedAndNotAsTheRewriteLeftIt(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "reasoning_effort", ValueFrom: "thinking.effort"},
		{Route: chatRoute, Key: "thinking.effort", Drop: true},
	}}

	got, err := rewriteBody(policy, chatRoute,
		[]byte(`{"model":"m","thinking":{"type":"enabled","effort":"low"},"reasoning_effort":"max"}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	assertSameJSON(t, got, `{"model":"m","thinking":{"type":"enabled"},"reasoning_effort":"low"}`)
}

// A copy never creates the parent it would write into, the same restraint a
// default keeps and for the same reason: inventing `chat_template_kwargs` on a
// request that never mentioned it would change what that caller gets.
func TestACarryNeverInventsTheParentItWritesInto(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Key: "chat_template_kwargs.thinking_effort", ValueFrom: "thinking.effort"},
	}}

	const body = `{"model":"m","thinking":{"effort":"low"}}`
	got, err := rewriteBody(policy, chatRoute, []byte(body))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if string(got) != body {
		t.Fatalf("body was rewritten:\n got %s\nwant %s", got, body)
	}

	got, err = rewriteBody(policy, chatRoute,
		[]byte(`{"model":"m","thinking":{"effort":"low"},"chat_template_kwargs":{}}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	assertSameJSON(t, got,
		`{"model":"m","thinking":{"effort":"low"},"chat_template_kwargs":{"thinking_effort":"low"}}`)
}

// Dropping the last key out of an object leaves the object. An empty `thinking`
// and no `thinking` at all are different requests, and a rewrite that turned one
// into the other would be answering a question nobody asked it.
func TestADropNeverRemovesTheParentItEmptied(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Key: "thinking.effort", Drop: true},
	}}

	got, err := rewriteBody(policy, chatRoute, []byte(`{"model":"m","thinking":{"effort":"low"}}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	assertSameJSON(t, got, `{"model":"m","thinking":{}}`)
}

// A key that is not there is not dropped, and a body nothing was taken out of is
// forwarded as the exact bytes that arrived.
func TestADropOfAnAbsentKeyDoesNotRewriteTheBody(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Key: "reasoning_effort", Drop: true},
		{Key: "thinking.effort", Drop: true},
	}}

	for _, body := range []string{
		`{"model":"m","messages":[{"role":"user","content":"hi"}]}`,
		`{"model":"m","thinking":{"type":"enabled"}}`,
		`{"model":"m","thinking":"not an object"}`,
	} {
		got, err := rewriteBody(policy, chatRoute, []byte(body))
		if err != nil {
			t.Fatalf("rewrite: %v", err)
		}
		if string(got) != body {
			t.Fatalf("body was rewritten:\n got %s\nwant %s", got, body)
		}
	}
}

// `when_present` is a scope and not a term, so it narrows every kind of rule and
// not only the one it was added for. This is the reason it sits beside `route`
// and `stream` rather than beside `drop`.
func TestWhenPresentNarrowsEveryKindOfRule(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{WhenPresent: "thinking", Key: "temperature", Enum: []any{float64(1)}},
		{WhenPresent: "thinking", Predicate: "tool_choice.named_while_thinking", Deny: true},
		{WhenPresent: "thinking", Key: "top_p", Default: 0.95},
	}}

	// With the guard satisfied, all three do their jobs.
	if _, _, ok := checkPolicy(policy, chatRoute, []byte(`{"thinking":{},"temperature":0.7}`)); ok {
		t.Fatal("the value term must apply when its condition holds")
	}
	refused := `{"thinking":{},"tool_choice":{"type":"function","function":{"name":"f"}}}`
	if _, _, ok := checkPolicy(policy, chatRoute, []byte(refused)); ok {
		t.Fatal("the predicate must apply when its condition holds")
	}
	got, err := rewriteBody(policy, chatRoute, []byte(`{"thinking":{}}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	assertSameJSON(t, got, `{"thinking":{},"top_p":0.95}`)

	// Without it, none of them applies.
	if _, message, ok := checkPolicy(policy, chatRoute, []byte(`{"temperature":0.7}`)); !ok {
		t.Fatalf("the condition does not hold: %s", message)
	}
	body := `{"tool_choice":{"type":"function","function":{"name":"f"}}}`
	if _, _, ok := checkPolicy(policy, chatRoute, []byte(body)); !ok {
		t.Fatal("the predicate must not fire when its condition does not hold")
	}
	got, err = rewriteBody(policy, chatRoute, []byte(`{"model":"m"}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if string(got) != `{"model":"m"}` {
		t.Fatalf("body = %s, want it untouched", got)
	}
}

// Which rules apply is decided once, against the body as it arrived. Without
// that, a rule guarded on a key another rule drops would fire or not depending on
// which of the two ran first — and the order of the list is not part of this
// language.
func TestScopeIsDecidedAgainstTheBodyThatArrived(t *testing.T) {
	rules := []plane.Rule{
		{Key: "reasoning_effort", Drop: true},
		{WhenPresent: "reasoning_effort", Key: "thinking.effort", Default: "max"},
	}
	forwards := &plane.Policy{Version: 2, Rules: rules}
	backwards := &plane.Policy{Version: 2, Rules: []plane.Rule{rules[1], rules[0]}}

	const body = `{"model":"m","reasoning_effort":"low","thinking":{}}`
	const want = `{"model":"m","thinking":{"effort":"max"}}`

	for _, policy := range []*plane.Policy{forwards, backwards} {
		got, err := rewriteBody(policy, chatRoute, []byte(body))
		if err != nil {
			t.Fatalf("rewrite: %v", err)
		}
		assertSameJSON(t, got, want)
	}
}

// End to end, because a term nothing consults is a term that does not exist: the
// drop has to reach the engine, and the request that carries no condition for it
// has to arrive as the caller wrote it.
func TestADropReachesTheUpstream(t *testing.T) {
	server, _ := upstreamServer(t)

	conforming := routeTo(t, server.URL, nil)
	conforming.Policy = &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, WhenPresent: "thinking.effort", Key: "reasoning_effort", Drop: true},
	}}
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"conforming": conforming,
	}}})

	send := func(body string) (int, string) {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, chatRoute, strings.NewReader(body)))
		return rec.Code, rec.Body.String()
	}

	code, seen := send(`{"model":"conforming","thinking":{"effort":"low"},"reasoning_effort":"max"}`)
	if code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", code, seen)
	}
	if strings.Contains(seen, "reasoning_effort") {
		t.Fatalf("body = %s, want the ignored field taken out before the engine saw it", seen)
	}
	assertSameJSON(t, []byte(seen), `{"model":"conforming","thinking":{"effort":"low"}}`)

	body := `{"model":"conforming","reasoning_effort":"max"}`
	code, seen = send(body)
	if code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", code, seen)
	}
	if seen != body {
		t.Fatalf("body = %s, want it forwarded untouched", seen)
	}
}

// mediaPolicy is the one rule these tests are about.
var mediaPolicy = &plane.Policy{Version: 2, Rules: []plane.Rule{
	{Route: chatRoute, Predicate: "messages.media", Deny: true},
}}

// A real MP4 header is eight bytes and this rule reads exactly those, so the
// fixtures are the signatures themselves rather than files. Base64 of
// "\0\0\0\x18ftypisom" and of "\x1a\x45\xdf\xa3" plus padding.
const (
	mp4Header  = "AAAAGGZ0eXBpc29t"
	webmHeader = "GkXfowAAAAAAAAAA"
	// The four zero bytes the conformance corpus and the vendor's own probe send.
	stubBytes = "AAAAAA=="
)

func mediaBody(part string) []byte {
	return []byte(`{"model":"moonshotai/kimi-k3","messages":[{"role":"user","content":[` +
		`{"type":"text","text":"what is in this"},` + part + `]}]}`)
}

// The defect. A video whose bytes cannot be a container reached the worker, which
// raised a Python exception with no status on it; the frontend had nothing to
// triage and answered 500 `Failed to generate completions` to a request that was
// the caller's mistake, as measured on a live deployment.
func TestAnUndecodableVideoIsRefusedRatherThanForwarded(t *testing.T) {
	for _, tc := range []struct {
		name string
		part string
		says string
	}{
		{
			"the four zero bytes the vendor's own probe sends",
			`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,` + stubBytes + `"}}`,
			"too few",
		},
		{
			"base64 that lost its last block",
			`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,AAAAGGZ0eXBpc29"}}`,
			"not valid base64",
		},
		{
			"an empty payload",
			`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,"}}`,
			"",
		},
		{
			"enough bytes, and no container among them",
			`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,` +
				"bm90IGEgdmlkZW8gYXQgYWxs" + `"}}`,
			"recognises",
		},
		{
			"audio, by the part name OpenAI gives it",
			`{"type":"input_audio","input_audio":{"data":"` + stubBytes + `","format":"wav"}}`,
			"too few",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			code, message, ok := checkPolicy(mediaPolicy, chatRoute, mediaBody(tc.part))
			if ok {
				t.Fatal("the request was forwarded, and the engine answers 500 for it")
			}
			if code != "invalid_parameter" {
				t.Errorf("code = %q, want invalid_parameter so the status is 400", code)
			}
			if tc.says != "" && !strings.Contains(message, tc.says) {
				t.Errorf("message %q does not say %q", message, tc.says)
			}
			if !strings.Contains(message, "messages[0].content[1]") {
				t.Errorf("message %q does not say which part is wrong", message)
			}
		})
	}
}

// The other half, and the half that costs a customer traffic if it is wrong.
func TestMediaThisRuleHasNoStandingOverIsForwarded(t *testing.T) {
	for _, tc := range []struct {
		name string
		part string
	}{
		{
			"an MP4 that is what it says it is",
			`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,` + mp4Header + `"}}`,
		},
		{
			"a WebM, whose magic is at offset zero rather than four",
			`{"type":"video_url","video_url":{"url":"data:video/webm;base64,` + webmHeader + `"}}`,
		},
		{
			// A container is not a modality. Refusing an MP4 under `video_url`
			// for carrying audio, or the reverse, would refuse a file the engine
			// plays.
			"audio inside an MP4",
			`{"type":"input_audio","input_audio":{"data":"` + mp4Header + `","format":"m4a"}}`,
		},
		{
			// Fetching an address a caller supplied is an egress decision and
			// this rule reads the request it was given.
			"a remote reference, which is nobody's bytes to judge here",
			`{"type":"video_url","video_url":{"url":"https://example.com/clip.mp4"}}`,
		},
		{
			// The percent-encoded data URL is legal and this rule cannot read it.
			"a data URL that is not base64",
			`{"type":"video_url","video_url":{"url":"data:video/mp4,%00%00%00%18ftypisom"}}`,
		},
		{
			// An image is decoded by the frontend, which already answers 400 for
			// one it cannot read. A second opinion here would be the format list
			// deciding what the decoder already decides correctly.
			"an image, which this rule does not read at all",
			`{"type":"image_url","image_url":{"url":"data:image/png;base64,` + stubBytes + `"}}`,
		},
		{
			"a shape fault rather than an encoding one, which the upstream words better",
			`{"type":"video_url","video_url":{}}`,
		},
		{
			"a part this build has never heard of",
			`{"type":"hologram_url","hologram_url":{"url":"data:model/obj;base64,` + stubBytes + `"}}`,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, message, ok := checkPolicy(mediaPolicy, chatRoute, mediaBody(tc.part)); !ok {
				t.Fatalf("refused a request it has no standing over: %s", message)
			}
		})
	}
}

// A string content carries no media, and an Anthropic body carries its own part
// names. Neither may be refused by a rule about OpenAI media parts — the cost of
// being wrong here is every request against the endpoint.
func TestBodiesWithoutOpenAIMediaPartsAreUntouched(t *testing.T) {
	for _, body := range []string{
		`{"model":"moonshotai/kimi-k3","messages":[{"role":"user","content":"hi"}]}`,
		`{"model":"moonshotai/kimi-k3","max_tokens":16,"messages":[{"role":"user",
		  "content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"` + stubBytes + `"}}]}]}`,
		`{"model":"moonshotai/kimi-k3","input":"hi"}`,
	} {
		if _, message, ok := checkPolicy(mediaPolicy, chatRoute, []byte(body)); !ok {
			t.Fatalf("refused a body carrying no OpenAI media part: %s\n%s", message, body)
		}
	}
}

// The refusal has to stay legible to the conformance board, which reads the
// message to tell "this endpoint has no video route" apart from "this endpoint
// has one and disliked the bytes". Getting this wrong reports an endpoint that
// serves video as one that cannot.
func TestTheRefusalReadsAsAPayloadFaultAndNotAsAMissingModality(t *testing.T) {
	part := `{"type":"video_url","video_url":{"url":"data:video/mp4;base64,` + stubBytes + `"}}`
	_, message, ok := checkPolicy(mediaPolicy, chatRoute, mediaBody(part))
	if ok {
		t.Fatal("the request was forwarded")
	}
	lower := strings.ToLower(message)

	// hgrconform's PAYLOAD vocabulary: one of these has to appear.
	payload := []string{"invalid", "prepare", "decode", "corrupt", "unsupported", "failed to load", "truncated"}
	found := false
	for _, needle := range payload {
		if strings.Contains(lower, needle) {
			found = true
		}
	}
	if !found {
		t.Errorf("message %q matches no payload-fault needle, so the video row collapses to `other`", message)
	}

	// And none of the vocabulary that means the modality was never configured.
	for _, needle := range []string{"at most 0", "may be provided", "limit_mm", "limit-mm", "not enabled", "does not support"} {
		if strings.Contains(lower, needle) {
			t.Errorf("message %q reads as a capped modality because of %q", message, needle)
		}
	}
}
