package serving

import (
	"encoding/json"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

// planeWire is the plane's own msgspec output for a Kimi K3 policy, byte for
// byte, captured from app.modules.endpoints.services.parameter_policy.
//
// Note the explicit nulls: msgspec writes every field of ParameterRuleSchema,
// so `min` and `max` arrive as null rather than absent. Both must decode to a
// nil *float64 and constrain nothing — if they ever decoded to 0 instead, every
// pinned parameter would silently gain a floor of zero.
const planeWire = `{"version":1,"parameters":{` +
	`"temperature":{"enum":[1],"min":null,"max":null},` +
	`"top_p":{"enum":[0.95],"min":null,"max":null},` +
	`"presence_penalty":{"enum":[0],"min":null,"max":null},` +
	`"frequency_penalty":{"enum":[0],"min":null,"max":null},` +
	`"logprobs":{"enum":[false],"min":null,"max":null}},` +
	`"deny":["tool_choice.named_while_thinking"]}`

// The plane serialises a policy; this build has to enforce it. These are two
// languages either side of one wire contract, and nothing else checks they agree.
func TestPlaneWireContract(t *testing.T) {
	var policy plane.ParameterPolicy
	if err := json.Unmarshal([]byte(planeWire), &policy); err != nil {
		t.Fatalf("plane JSON does not decode: %v", err)
	}
	if len(policy.Parameters) != 5 {
		t.Fatalf("parameters = %d, want 5", len(policy.Parameters))
	}
	if rule := policy.Parameters["temperature"]; rule.Min != nil || rule.Max != nil {
		t.Fatal("an explicit null must decode to no constraint, not to zero")
	}

	// Exactly what Moonshot answered 400 to in the 2026-09-01 comparison.
	for _, refused := range []string{
		`{"model":"m","temperature":0.7}`,
		`{"model":"m","top_p":0.9}`,
		`{"model":"m","presence_penalty":0.5}`,
		`{"model":"m","frequency_penalty":0.5}`,
		`{"model":"m","logprobs":true}`,
		`{"model":"m","tool_choice":{"type":"function","function":{"name":"f"}}}`,
		`{"model":"m","tool_choice":{"type":"function","function":{"name":"f"}},"thinking":{"type":"enabled"}}`,
	} {
		if _, _, ok := checkPolicy(&policy, []byte(refused)); ok {
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
		if _, _, ok := checkPolicy(&policy, []byte(accepted)); !ok {
			t.Fatalf("%s: the provider serves this", accepted)
		}
	}
}
