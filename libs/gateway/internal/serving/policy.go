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

// diagnoses are the denials that answer in their own words.
//
// A policy names a rule from here exactly as it names one from `denials`, and
// the two are looked up in the same pass. What differs is who the refusal is
// about. A denial is about the endpoint — "this model does not accept feature
// X" — and one sentence covers it however the request was written. A diagnosis
// is about the request: which declaration, and what is wrong with it. Answering
// that with the generic sentence would tell a customer whose tool has a typo in
// it that a feature they are entitled to does not exist, and leave them nothing
// to correct, so a rule here returns its own code and message.
//
// Everything `denials` promises still holds here. A name this build does not
// carry is ignored rather than enforced, and a shape a rule cannot positively
// read is left to the upstream.
var diagnoses = map[string]func(map[string]json.RawMessage) (code string, message string, refused bool){
	// Named for the JSON path it constrains — `tools` on an element of
	// `messages` — which is how the tool_choice rule above is named too. The
	// request-level `tools` array is a different path and a different feature,
	// and this rule reads it only far enough to compare names, never to judge
	// its shape: that array is served today, and a rule that quietly acquired
	// an opinion about it would break traffic nobody asked about.
	"messages.tools": malformedMessageTools,
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

// maxToolNameLength is the longest function name K3 takes. It is deliberately
// not OpenAI's 64: this endpoint sells consistency with Moonshot, and narrowing
// the bound to the one most clients assume would refuse names the provider
// serves.
const maxToolNameLength = 256

// malformedMessageTools reports a tool declared on a message in a shape K3
// answers 400 to, and says which declaration and why.
//
// K3 lets a caller attach tools to a `system` message instead of, or as well
// as, the request-level array. The engine behind this endpoint does not read
// that field at all — it drops it on the way through — so every malformed
// declaration currently gets a 200 with the tool silently missing, and nothing
// below this line will ever catch one. That is the gap this rule closes.
//
// Measured against api.moonshot.ai, model kimi-k3, on 2026-09-04. Refused:
//
//	tools on a user or on an assistant message
//	tools beside non-empty content on a system message
//	tools that is not an array, or an item of it that is not an object
//	a tool with no type, no function, or no function.name
//	a type other than "function", including on one item of an otherwise good list
//	a name that is empty, starts with a digit, holds "@", or runs past 256
//	a name repeated within a message, across two messages, or against the
//	  request-level tools array
//
// Served, and so never refused here: a declaration on any system position
// including the last message, three tools on one message, two messages carrying
// one each, and a message-level name beside a different request-level one.
//
// Two rules are wider than the cell that produced them, because that cell is a
// sample of a class and not the class itself. "@" stands for every character
// outside `[A-Za-z0-9_-]`; the hyphen is in that set on the OpenAI charset this
// endpoint mirrors rather than on a measurement, and leaving it out would
// refuse most of the hyphenated tool names in circulation. `null` stands for
// every item that is not an object. Nothing else is widened — in particular
// `parameters` is not required, because the vendor's own missing-field matrix
// covers type, function and name and stops there, and demanding it would refuse
// a tool that takes no arguments.
func malformedMessageTools(fields map[string]json.RawMessage) (string, string, bool) {
	messages, readable := jsonArray(fields["messages"])
	if !readable {
		return "", "", false
	}

	declared := requestToolNames(fields["tools"])

	for index, raw := range messages {
		message, readable := jsonObject(raw)
		if !readable {
			continue
		}
		tools, carried := message["tools"]
		if !carried || isJSONNull(tools) {
			// An explicit null is read as no declaration rather than as a
			// broken one. Plenty of clients serialise an absent optional that
			// way, and refusing them would break requests that carry no tools
			// at all and never meant to.
			continue
		}
		role, stated := jsonString(message["role"])
		if !stated {
			// A message this rule cannot place is not one it can judge. The
			// upstream will answer for the missing role.
			continue
		}
		if role != "system" {
			return refusal("Only a system message may declare tools, and messages[%d] has role %q.", index, role)
		}
		if content, isText := jsonString(message["content"]); isText && content != "" {
			// Only a string content is judged. A content sent as an array of
			// parts is a shape this rule was never measured against, and
			// guessing at "empty" for it would refuse a request the model may
			// well serve.
			return refusal("A system message that declares tools must leave its content empty, and messages[%d] does not.", index)
		}
		items, isArray := jsonArray(tools)
		if !isArray {
			return refusal("The 'tools' on messages[%d] must be an array.", index)
		}
		for position, item := range items {
			name, fault := toolDeclaration(item)
			if fault != "" {
				return refusal("messages[%d].tools[%d] %s", index, position, fault)
			}
			if declared[name] {
				return refusal("messages[%d].tools[%d] declares the tool name %q a second time, and a name may be declared once per request.", index, position, name)
			}
			declared[name] = true
		}
	}

	return "", "", false
}

// refusal builds the answer a diagnosis hands back.
//
// The code is the one this package already uses for a value it will not take,
// rather than a synonym invented for this rule: from the caller's side both are
// "what you sent for this parameter is not acceptable", and two codes for one
// thing only makes the surface a client has to branch on wider.
func refusal(format string, args ...any) (string, string, bool) {
	return "invalid_parameter", fmt.Sprintf(format, args...), true
}

// toolDeclaration reads one declaration, returning either its name or the
// sentence that says what is wrong with it.
//
// The two are exclusive on purpose: a name is what the uniqueness check needs,
// and a tool that has no readable name has already failed for its own reason,
// so there is never a name to carry forward from a broken one.
func toolDeclaration(raw json.RawMessage) (name string, fault string) {
	tool, readable := jsonObject(raw)
	if !readable {
		return "", "is not an object."
	}
	kind, typed := tool["type"]
	if !typed {
		return "", "has no 'type'."
	}
	if value, isText := jsonString(kind); !isText || value != "function" {
		return "", "is not a function tool."
	}
	declaration, present := tool["function"]
	if !present {
		return "", "has no 'function'."
	}
	function, readable := jsonObject(declaration)
	if !readable {
		return "", "has a 'function' that is not an object."
	}
	given, present := function["name"]
	if !present {
		return "", "has no 'function.name'."
	}
	name, isText := jsonString(given)
	if !isText {
		return "", "has a 'function.name' that is not a string."
	}
	if name == "" {
		return "", "has an empty 'function.name'."
	}
	if len(name) > maxToolNameLength {
		// The length is reported instead of the name so that a caller cannot
		// make this hop echo an unbounded string back out of itself.
		return "", fmt.Sprintf("has a 'function.name' of %d characters, and %d is the most this model takes.", len(name), maxToolNameLength)
	}
	if !isToolName(name) {
		return "", fmt.Sprintf("has the tool name %q, which this model does not accept.", name)
	}
	return name, ""
}

// isToolName reports a name K3 will take, length aside.
//
// A digit is allowed everywhere except first. That asymmetry is the vendor's,
// not a typo here: `1bad_name` is refused where `bad_name1` is not, and a rule
// that banned digits outright would refuse the version-suffixed names that tool
// authors write all the time.
func isToolName(name string) bool {
	for position, character := range name {
		switch {
		case character >= 'a' && character <= 'z',
			character >= 'A' && character <= 'Z',
			character == '_', character == '-':
		case character >= '0' && character <= '9':
			if position == 0 {
				return false
			}
		default:
			return false
		}
	}
	return true
}

// requestToolNames collects the names the request-level `tools` array already
// spends, so that a message-level declaration can be checked against them.
//
// Nothing here refuses anything. A request-level array that is malformed, or
// that repeats a name inside itself, is the upstream's business and is served
// today; reading it for names only means this rule can enforce the collision
// the vendor measured without quietly acquiring an opinion about a feature it
// was never asked to police.
func requestToolNames(raw json.RawMessage) map[string]bool {
	names := make(map[string]bool)
	items, isArray := jsonArray(raw)
	if !isArray {
		return names
	}
	for _, item := range items {
		if name, fault := toolDeclaration(item); fault == "" {
			names[name] = true
		}
	}
	return names
}

// jsonArray, jsonObject, jsonString and isJSONNull each answer "is this the
// shape I need" separately from "what is in it", for the same reason
// objectField does: a field this rule cannot read must stay distinguishable
// from one that is simply absent, or a malformed request and a default one
// become the same request.
func jsonArray(raw json.RawMessage) ([]json.RawMessage, bool) {
	if len(raw) == 0 {
		return nil, false
	}
	var values []json.RawMessage
	if err := json.Unmarshal(raw, &values); err != nil {
		return nil, false
	}
	return values, true
}

// A JSON null decodes into a map without error, leaving it nil. Letting that
// through as a readable object would turn `tools: [null]` into a tool that is
// merely missing its fields, and the caller would be told to add a `type` to a
// declaration they never wrote.
func jsonObject(raw json.RawMessage) (map[string]json.RawMessage, bool) {
	if len(raw) == 0 {
		return nil, false
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil || object == nil {
		return nil, false
	}
	return object, true
}

func jsonString(raw json.RawMessage) (string, bool) {
	if len(raw) == 0 {
		return "", false
	}
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", false
	}
	return value, true
}

func isJSONNull(raw json.RawMessage) bool {
	var value any
	return json.Unmarshal(raw, &value) == nil && value == nil
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
		if refuse, known := denials[name]; known {
			if refuse(fields) {
				return "unsupported_parameter", "The request uses a feature this model does not accept.", false
			}
			continue
		}
		if diagnose, known := diagnoses[name]; known {
			if code, message, refused := diagnose(fields); refused {
				return code, message, false
			}
			continue
		}
		// Reported at refresh time, not here. A rule this build cannot read is
		// a conformance gap somebody must see, never a reason to drop a request
		// that may be perfectly fine.
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
