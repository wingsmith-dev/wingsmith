package serving

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

// rule is one line of the plane's own msgspec output, byte for byte. Every
// field is written even when it is empty, because msgspec writes the whole
// struct, so `min` and `max` arrive as null, not absent, `route` and
// `when_present` as "", and `stream` as null. Every one of them must reach this
// build as "asks nothing": a null min that decoded to 0 would give every pinned
// parameter a silent floor of zero, and an empty route or condition that did not
// match everything would turn every migrated policy off.
func rule(fields string) string {
	return "{" + fields + "}"
}

// planeWire is what app.modules.endpoints.services.endpoint_policy serialises
// for the reference endpoint: the pinned parameters, the tool_choice denial, the
// `thinking.type` default and the `reasoning_effort` drop: every one of them
// scoped to the path it was measured on.
var planeWire = `{"version":2,"rules":[` + strings.Join([]string{
	rule(`"route":"/v1/chat/completions","stream":null,"when_present":"","key":"temperature","predicate":"","enum":[1],"min":null,"max":null,"deny":false,"drop":false,"default":null`),
	rule(`"route":"/v1/chat/completions","stream":null,"when_present":"","key":"top_p","predicate":"","enum":[0.95],"min":null,"max":null,"deny":false,"drop":false,"default":null`),
	rule(`"route":"/v1/chat/completions","stream":null,"when_present":"","key":"presence_penalty","predicate":"","enum":[0],"min":null,"max":null,"deny":false,"drop":false,"default":null`),
	rule(`"route":"/v1/chat/completions","stream":null,"when_present":"","key":"frequency_penalty","predicate":"","enum":[0],"min":null,"max":null,"deny":false,"drop":false,"default":null`),
	rule(`"route":"/v1/chat/completions","stream":null,"when_present":"","key":"logprobs","predicate":"","enum":[false],"min":null,"max":null,"deny":false,"drop":false,"default":null`),
	rule(`"route":"/v1/chat/completions","stream":null,"when_present":"","key":"","predicate":"tool_choice.named_while_thinking","enum":null,"min":null,"max":null,"deny":true,"drop":false,"default":null`),
	rule(`"route":"/v1/chat/completions","stream":null,"when_present":"","key":"thinking.type","predicate":"","enum":null,"min":null,"max":null,"deny":false,"drop":false,"default":"enabled"`),
	rule(`"route":"/v1/chat/completions","stream":null,"when_present":"thinking.effort","key":"reasoning_effort","predicate":"","enum":null,"min":null,"max":null,"deny":false,"drop":true,"default":null`),
}, ",") + `]}`

// migratedWire is the same function's output for a policy still stored in the
// v1 shape, a map of parameters and a list of denial names, which is what
// every endpoint that sold a term before this language existed still holds.
//
// The conversion is the plane's, so this side never learns two languages. Note
// what it does not do: the rules come out unscoped, because v1 had no route and
// a migration that narrowed a sold term to one path would be deciding something
// its author never said.
var migratedWire = `{"version":2,"rules":[` + strings.Join([]string{
	rule(`"route":"","stream":null,"when_present":"","key":"frequency_penalty","predicate":"","enum":[0],"min":null,"max":null,"deny":false,"drop":false,"default":null`),
	rule(`"route":"","stream":null,"when_present":"","key":"logprobs","predicate":"","enum":[false],"min":null,"max":null,"deny":false,"drop":false,"default":null`),
	rule(`"route":"","stream":null,"when_present":"","key":"presence_penalty","predicate":"","enum":[0],"min":null,"max":null,"deny":false,"drop":false,"default":null`),
	rule(`"route":"","stream":null,"when_present":"","key":"temperature","predicate":"","enum":[1],"min":null,"max":null,"deny":false,"drop":false,"default":null`),
	rule(`"route":"","stream":null,"when_present":"","key":"top_p","predicate":"","enum":[0.95],"min":null,"max":null,"deny":false,"drop":false,"default":null`),
	rule(`"route":"","stream":null,"when_present":"","key":"","predicate":"tool_choice.named_while_thinking","enum":null,"min":null,"max":null,"deny":true,"drop":false,"default":null`),
}, ",") + `]}`

// The plane serialises a policy; this build has to enforce it. These are two
// languages either side of one wire contract, and nothing else checks they agree.
func TestPlaneWireContract(t *testing.T) {
	var policy plane.Policy
	if err := json.Unmarshal([]byte(planeWire), &policy); err != nil {
		t.Fatalf("plane JSON does not decode: %v", err)
	}
	if len(policy.Rules) != 8 {
		t.Fatalf("rules = %d, want 8", len(policy.Rules))
	}
	for _, r := range policy.Rules {
		if r.Min != nil || r.Max != nil {
			t.Fatalf("an explicit null must decode to no constraint, not to zero: %+v", r)
		}
		if r.Stream != nil {
			t.Fatalf("a null stream must decode to both modes: %+v", r)
		}
	}

	// Exactly what the reference provider's API answered 400 to.
	for _, refused := range []string{
		`{"model":"m","temperature":0.7}`,
		`{"model":"m","top_p":0.9}`,
		`{"model":"m","presence_penalty":0.5}`,
		`{"model":"m","frequency_penalty":0.5}`,
		`{"model":"m","logprobs":true}`,
		`{"model":"m","tool_choice":{"type":"function","function":{"name":"f"}}}`,
		`{"model":"m","tool_choice":{"type":"function","function":{"name":"f"}},"thinking":{"type":"enabled"}}`,
	} {
		if _, _, ok := checkPolicy(&policy, chatRoute, []byte(refused)); ok {
			t.Fatalf("%s should have been refused", refused)
		}
	}

	// And what it answered 200 to. The last two are the cells a coarser rule
	// would have refused: a named function with thinking off, and allowed_tools,
	// which is an object tool_choice the provider serves in both modes.
	good := `{"model":"m","temperature":1,"top_p":0.95,"presence_penalty":0,` +
		`"frequency_penalty":0,"logprobs":false,"tool_choice":"auto"}`
	for _, accepted := range []string{
		good,
		`{"model":"m","tool_choice":{"type":"function","function":{"name":"f"}},"thinking":{"type":"disabled"}}`,
		`{"model":"m","tool_choice":{"type":"allowed_tools","allowed_tools":{"mode":"auto"}}}`,
	} {
		if _, _, ok := checkPolicy(&policy, chatRoute, []byte(accepted)); !ok {
			t.Fatalf("%s: the provider serves this", accepted)
		}
	}

	// Every one of those pins was measured on chat completions and now says so,
	// so none of them reaches another protocol. This is the whole of the fix:
	// before a rule carried a route, the first body here was a 400 on an
	// endpoint that had sold nothing about embeddings at all.
	for _, elsewhere := range []struct{ route, body string }{
		{embeddingsRoute, `{"model":"m","input":"hello","temperature":0.7}`},
		{messagesRoute, `{"model":"m","max_tokens":16,"temperature":0.7,"messages":[]}`},
		{responsesRoute, `{"model":"m","input":"hello","top_p":0.9}`},
	} {
		if _, message, ok := checkPolicy(&policy, elsewhere.route, []byte(elsewhere.body)); !ok {
			t.Fatalf("%s: a chat-completions term must not reach it: %s", elsewhere.route, message)
		}
	}

	// The default reaches the wire as a default and fills the field the vendor
	// treats as having one.
	filled, err := rewriteBody(&policy, chatRoute, []byte(`{"model":"m","thinking":{"keep":"all"}}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if !strings.Contains(string(filled), `"type":"enabled"`) {
		t.Fatalf("body = %s, want thinking.type filled in", filled)
	}

	// And the drop reaches it as a drop, on the condition it was written with.
	dropped, err := rewriteBody(&policy, chatRoute,
		[]byte(`{"model":"m","thinking":{"effort":"low"},"reasoning_effort":"max"}`))
	if err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	if strings.Contains(string(dropped), "reasoning_effort") {
		t.Fatalf("body = %s, want reasoning_effort taken out", dropped)
	}
}

// A policy written before this language existed still has to be enforced, and
// the plane is where it is translated. What arrives here is v2 either way.
func TestMigratedPolicyStillEnforcesWhatItSold(t *testing.T) {
	var policy plane.Policy
	if err := json.Unmarshal([]byte(migratedWire), &policy); err != nil {
		t.Fatalf("migrated JSON does not decode: %v", err)
	}
	if policy.Version != 2 {
		t.Fatalf("version = %d, want the shape the plane emits", policy.Version)
	}

	for _, refused := range []string{
		`{"model":"m","temperature":0.7}`,
		`{"model":"m","logprobs":true}`,
		`{"model":"m","tool_choice":{"type":"function","function":{"name":"f"}}}`,
	} {
		if _, _, ok := checkPolicy(&policy, chatRoute, []byte(refused)); ok {
			t.Fatalf("%s: a migrated policy must still refuse what it sold", refused)
		}
	}

	// And it still reaches every path, because that is what it meant before. The
	// leak is closed by writing a route down, which is an edit an operator makes,
	// not something a migration decides on their behalf.
	if _, _, ok := checkPolicy(&policy, embeddingsRoute, []byte(`{"model":"m","temperature":0.7}`)); ok {
		t.Fatal("an unscoped rule matches every path, which is exactly what v1 meant")
	}
}
