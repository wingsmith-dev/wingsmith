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
// A name here describes a refusal in the terms a policy author can check
// against the provider. Most are pure OpenAI wire concepts; one names a vendor
// extension, because the thing being constrained *is* a vendor extension and
// pretending otherwise would leave the rule unwritable. Which endpoint carries
// which name stays the plane's business and never this package's.
//
// A predicate reads the whole body, so a rule may be a conjunction of more than
// one field. It refuses only what it can positively identify: a shape it does
// not recognise is left to the upstream, which will answer for it. Refusing on
// a guess trades a conformance gap for a broken request.
var denials = map[string]func(map[string]json.RawMessage) bool{
	// Moonshot answers this one 400 `invalid_request_error`, with the message
	// "tool_choice 'specified' is incompatible with thinking enabled".
	// Measured against api.moonshot.ai, model kimi-k3, on 2026-09-04. The full
	// matrix has exactly one refused cell:
	//
	//	tool_choice                     thinking on   thinking off
	//	auto / none / required          200           200
	//	{"type":"function",...}         400           200
	//	{"type":"allowed_tools",...}    200           200
	//
	// So the rule is neither "an object tool_choice" nor "a named function".
	// Denying either of those wider things would refuse a request the model
	// serves: allowed_tools in both modes, and a named function whenever the
	// caller has turned thinking off.
	"tool_choice.named_while_thinking": func(fields map[string]json.RawMessage) bool {
		return isNamedFunctionChoice(fields["tool_choice"]) && thinkingEnabled(fields)
	},
}

// isNamedFunctionChoice reports the `{"type":"function","function":{...}}` form.
//
// The `type` is what separates it from `allowed_tools`, which is also an object
// and is accepted in both thinking modes. The bare strings — auto, none,
// required — are a different thing again, and are never this.
func isNamedFunctionChoice(raw json.RawMessage) bool {
	value, _ := objectField(raw, "type")
	return value == "function"
}

// thinkingEnabled reports whether this request positively reaches the model
// thinking.
//
// Absent means enabled: K3 thinks unless it is told not to, so a caller who
// sends no `thinking` at all is in the refused cell. Two spellings turn it off,
// because two of them reach this gateway — Moonshot's `thinking.type`, and the
// open-source deployment's `chat_template_kwargs.thinking`, which is what the
// vLLM chat template reads. An endpoint may accept either, and a policy that
// understood only one would refuse a request whose caller had correctly
// switched thinking off.
//
// A field that is present but unreadable answers false, so the request is not
// refused here. It is already broken and the upstream will say so in its own
// words; rejecting it locally would replace the provider's reason with a worse
// one, and this rule has no standing to speak about a malformed `thinking`.
func thinkingEnabled(fields map[string]json.RawMessage) bool {
	if raw, sent := fields["thinking"]; sent {
		value, readable := objectField(raw, "type")
		if !readable || value == "disabled" {
			return false
		}
	}
	if raw, sent := fields["chat_template_kwargs"]; sent {
		value, readable := objectField(raw, "thinking")
		if !readable || value == false {
			return false
		}
	}
	return true
}

// objectField reads one field out of a JSON object, reporting separately
// whether the object could be read at all.
//
// The two answers are different questions and every caller here needs both: a
// field this rule cannot parse is not the same as an object that simply does
// not carry it, and collapsing them would make a malformed body indistinguish-
// able from a default one.
func objectField(raw json.RawMessage, name string) (value any, readable bool) {
	if len(raw) == 0 {
		return nil, false
	}
	var object map[string]any
	if err := json.Unmarshal(raw, &object); err != nil {
		return nil, false
	}
	return object[name], true
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
