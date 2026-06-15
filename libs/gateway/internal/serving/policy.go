package serving

import (
	"encoding/json"
	"fmt"
	"sort"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

// supportedPolicyVersion is the highest policy version this build understands.
// A table carrying a higher one is enforced as far as it is understood and
// reported once per refresh, in routing.
const supportedPolicyVersion = 1

// denials are the wire features a policy may refuse by name.
//
// Every name here is an OpenAI wire concept, never a vendor's. "this endpoint
// refuses a named function in tool_choice" is a statement about the protocol;
// which endpoint says it is the plane's business and never this package's.
var denials = map[string]func(map[string]json.RawMessage) bool{
	// A named function is `{"type":"function","function":{...}}`. The bare
	// strings — auto, none, required — are a different thing and stay allowed.
	"tool_choice.named": func(fields map[string]json.RawMessage) bool {
		raw, ok := fields["tool_choice"]
		if !ok {
			return false
		}
		var probe any
		if err := json.Unmarshal(raw, &probe); err != nil {
			return false
		}
		_, isObject := probe.(map[string]any)
		return isObject
	},
}

// checkPolicy reports the first violation of an endpoint's request terms, or
// ok when there is none.
//
// A nil policy returns immediately without touching the body, which is the
// whole point of putting this per endpoint: every endpoint that has not sold a
// consistency term pays one nil check and nothing else.
//
// An absent parameter is never a violation. A provider that pins `temperature`
// to 1 rejects the request that *sends* something else; it does not demand the
// field. Requiring it would refuse the minimal request every client starts with.
func checkPolicy(policy *plane.ParameterPolicy, body []byte) (code string, message string, ok bool) {
	if policy == nil {
		return "", "", true
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		// Not this check's business. A JSON body that fails here has already
		// failed where the caller read its `model`, and been reported there.
		//
		// A multipart body reaches this line intact and passes unchecked. Its
		// parameters are form fields, not JSON values, and a policy is written
		// against the JSON wire. Selling a consistency term on a multipart
		// endpoint needs this to learn parts first.
		return "", "", true
	}

	for _, name := range sortedKeys(policy.Parameters) {
		raw, sent := fields[name]
		if !sent {
			continue
		}
		var value any
		if err := json.Unmarshal(raw, &value); err != nil {
			continue
		}
		if allowed(policy.Parameters[name], value) {
			continue
		}
		return "invalid_parameter", fmt.Sprintf("The value for '%s' is not accepted by this model.", name), false
	}

	for _, name := range policy.Deny {
		refuse, known := denials[name]
		if !known {
			// Reported at refresh time, not here. A rule this build cannot read
			// is a conformance gap somebody must see, never a reason to drop a
			// request that may be perfectly fine.
			continue
		}
		if refuse(fields) {
			return "unsupported_parameter", "The request uses a feature this model does not accept.", false
		}
	}

	return "", "", true
}

// allowed reports whether one value satisfies one rule. An empty rule permits
// everything, which is what an absent constraint has to mean.
func allowed(rule plane.ParameterRule, value any) bool {
	if len(rule.Enum) > 0 && !inEnum(rule.Enum, value) {
		return false
	}
	if rule.Min != nil || rule.Max != nil {
		number, isNumber := value.(float64)
		if !isNumber {
			return false
		}
		if rule.Min != nil && number < *rule.Min {
			return false
		}
		if rule.Max != nil && number > *rule.Max {
			return false
		}
	}
	return true
}

// inEnum compares against the permitted set. JSON scalars decode to float64,
// bool, string or nil, all of which compare with ==. Anything else — an object
// or an array — is never equal to a scalar the policy named, and comparing it
// with == would panic on an unhashable type.
func inEnum(permitted []any, value any) bool {
	switch value.(type) {
	case float64, bool, string, nil:
	default:
		return false
	}
	for _, candidate := range permitted {
		if candidate == value {
			return true
		}
	}
	return false
}

// sortedKeys keeps the reported violation stable when a request breaks two
// rules at once. Go randomises map iteration, and a check that names a
// different parameter each time is one nobody can write a test against.
func sortedKeys(rules map[string]plane.ParameterRule) []string {
	names := make([]string, 0, len(rules))
	for name := range rules {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}
