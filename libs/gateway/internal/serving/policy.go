package serving

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

// supportedPolicyVersion is the highest policy version this build understands.
// A table carrying a higher one is enforced as far as it is understood and
// reported once per refresh, in routing.
//
// 2 is the scoped-rule shape: a list of rules, each with a route and a stream
// mode it applies to, a key or a predicate it is about, and its terms. 1 was a
// map of parameter names to constraints plus a flat list of denial names, with
// no way to say where a rule applied and no way to say what an absent field
// should become. The plane converts a stored v1 policy on the way out, so only
// one language reaches this file.
const supportedPolicyVersion = 2

// denials are the wire features a policy may refuse by name.
//
// A rule names one of these in its `predicate`, which is a namespace of its own
// and never a JSON path: a key is read out of the body, a predicate is looked
// up here. A name here describes a refusal in the terms a policy author can
// check against the provider. Most are pure OpenAI wire concepts; one names a vendor
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
// A rule names one from here exactly as it names one from `denials` — same
// `predicate` field, and the two are looked up in the same pass. What differs is who the refusal is
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
	// Named for the paths it reads — the media content parts on an element of
	// `messages`. It answers in its own words for the same reason the rule above
	// does: "this model does not accept video" is the wrong sentence to hand
	// somebody whose video is fine and whose base64 lost its last block.
	"messages.media": undecodableMessageMedia,
	// Named for the key it constrains and the thing that decides which value is
	// pinned, the way `tool_choice.named_while_thinking` is. It answers in its
	// own words because the accepted value is not a constant: a generic "the
	// value for 'temperature' is not accepted" would leave a caller who sent
	// 0.6 with thinking on unable to tell that 0.6 is the *only* value accepted
	// one line away, with thinking off.
	"temperature.not_pinned_for_mode": temperatureOffProviderPin,
}

// temperaturePins are the only temperature the provider accepts, per thinking mode.
//
// Measured against api.moonshot.ai, model kimi-k3, on 2026-09-11. The matrix is
// two cells wide and every other value in it is a 400:
//
//	temperature   thinking on   thinking off
//	1.0           200           400
//	0.6           400           200
//	0.0           400           400
//
// Which is a stricter rule than an `enum` on the key can express, and stricter
// than the vendor verifier's own table believes. `IMMUTABLE_PARAMS` asserts 0.6
// and 0.0 are both accepted with thinking on; the provider those tests describe
// refuses both. That contradiction is the whole reason this is a term an
// endpoint opts into rather than behaviour compiled in: one document asks for
// the table, the other asks for the provider, and which one an endpoint is sold
// against is a commercial decision.
//
// Thinking-off is the half nobody had looked at. The report that prompted this
// said "thinking-off is fine on both", which is true of 0.6 and of nothing
// else — 0.0 and 1.0 are refused there too, and are the cells a caller
// switching thinking off at runtime walks into first.
var temperaturePins = map[bool]float64{true: 1, false: 0.6}

// temperatureOffProviderPin refuses a temperature the provider would refuse, in
// the mode this request is actually in.
//
// An absent `temperature` is never a violation, here as everywhere else in this
// file: the provider pins the value a caller sends and does not demand the
// field. A present-but-unreadable one is left to the upstream for the same
// reason `thinkingEnabled` leaves a malformed `thinking` alone — it is already
// broken, and replacing the engine's reason with ours makes it harder to fix.
//
// `null` counts as unreadable and not as zero. Decoding it into a float64
// leaves the zero value behind and reports no error, which would refuse
// `temperature: null` as though the caller had asked for 0 — a request the
// provider serves, since a null field is an absent one on this wire.
//
// A malformed `thinking` makes this rule abstain outright rather than fall back
// to a mode. `thinkingEnabled` answers false for one, which is right for a
// refusal that only fires while thinking is on and wrong here: the mode is what
// *chooses* the pinned value, so reading it as "off" would refuse
// `temperature: 1` — correct in the mode the request is probably in — on the
// strength of a field nobody could parse.
func temperatureOffProviderPin(fields map[string]json.RawMessage) (string, string, bool) {
	raw, sent := fields["temperature"]
	if !sent {
		return "", "", false
	}
	var value *float64
	if err := json.Unmarshal(raw, &value); err != nil || value == nil {
		return "", "", false
	}
	if raw, present := fields["thinking"]; present {
		if _, readable := objectField(raw, "type"); !readable {
			return "", "", false
		}
	}
	thinking := thinkingEnabled(fields)
	pinned := temperaturePins[thinking]
	if *value == pinned {
		return "", "", false
	}
	mode := "disabled"
	if thinking {
		mode = "enabled"
	}
	return "invalid_parameter",
		fmt.Sprintf(
			"The only accepted value for 'temperature' is %v while thinking is %s.",
			pinned, mode,
		), true
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
// sends no `thinking` at all is in the refused cell.
//
// `thinking.type` is the only spelling read. `chat_template_kwargs.thinking` is
// what the vLLM chat template reads and it used to be read here too, on the
// grounds that a caller who had correctly switched thinking off should not be
// refused for it. That was true of the engine and never of the provider:
// Moonshot has no `chat_template_kwargs`, ignores it, and thinks anyway — so a
// request carrying it is in the *refused* cell there, and honouring it here
// answered 200 where the endpoint's own provider answers 400.
//
// The spelling is handled where an ignored field belongs, as a `drop` term on
// the endpoint's policy, which takes it out of the body before the engine can
// act on it. That makes the two halves agree: the field reaches neither the
// engine's behaviour nor this rule's reading of the request.
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

// checkPolicy reports the first violation of an endpoint's request terms on
// this route, or ok when there is none.
//
// A nil policy returns immediately without touching the body, which is the
// whole point of putting this per endpoint: every endpoint that has not sold a
// consistency term pays one nil check and nothing else.
//
// Every rule whose scope matches applies and the request must satisfy all of
// them. There is no precedence between rules and no most-specific-wins: a term
// is something that was sold, and a second term does not cancel the first. Two
// rules that cannot both hold are an authoring mistake, refused where the policy
// is written rather than resolved here on a request that is already in flight.
//
// An absent parameter is never a violation. A provider that pins `temperature`
// to 1 rejects the request that *sends* something else; it does not demand the
// field. Requiring it would refuse the minimal request every client starts with.
func checkPolicy(policy *plane.Policy, route string, body []byte) (code string, message string, ok bool) {
	if policy == nil || len(policy.Rules) == 0 {
		return "", "", true
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil || fields == nil {
		// Not this check's business. A JSON body that fails here has already
		// failed where the caller read its `model`, and been reported there.
		//
		// A multipart body reaches this line intact and passes unchecked. Its
		// parameters are form fields, not JSON values, and a policy is written
		// against the JSON wire. Selling a consistency term on a multipart
		// endpoint needs this to learn parts first.
		return "", "", true
	}

	for _, rule := range inScope(policy.Rules, route, fields) {
		switch {
		case rule.Predicate != "":
			if !rule.Deny {
				// A predicate says nothing about a value, so a rule naming one
				// without denying it constrains nothing. Refused at author time
				// in the plane; inert here.
				continue
			}
			if refuse, known := denials[rule.Predicate]; known {
				if refuse(fields) {
					return "unsupported_parameter",
						"The request uses a feature this model does not accept.", false
				}
				continue
			}
			if diagnose, known := diagnoses[rule.Predicate]; known {
				if code, message, refused := diagnose(fields); refused {
					return code, message, false
				}
				continue
			}
			// Reported at refresh time, not here. A rule this build cannot read
			// is a conformance gap somebody must see, never a reason to drop a
			// request that may be perfectly fine.

		case rule.Key != "":
			value, present := lookup(fields, rule.Key)
			if !present {
				continue
			}
			if rule.Deny {
				return "unsupported_parameter",
					fmt.Sprintf("The parameter '%s' is not accepted by this model.", rule.Key), false
			}
			if allowed(rule, value) {
				continue
			}
			return "invalid_parameter",
				fmt.Sprintf("The value for '%s' is not accepted by this model.", rule.Key), false
		}
	}

	return "", "", true
}

// rewriteBody returns the request with every writing term this route's rules
// declare applied to it, and the body untouched when there is nothing to write.
//
// It is separate from checkPolicy because the two do different things to a
// request and only one of them can fail it. A check reads and refuses; this
// writes and never refuses. Keeping them apart is what lets the check stay a
// pure function of the body the caller sent.
//
// Which rules apply is decided once, against the body as it arrived, and never
// re-decided as the rewrite proceeds. That is what makes the pass a single
// simultaneous edit rather than a sequence: a rule whose `when_present` names a
// key another rule drops would otherwise fire or not depending on which of them
// ran first, and the order of the list is not part of this language.
//
// The body is re-encoded only when something was actually written, so the
// ordinary request — which is nearly all of them — is forwarded as the exact
// bytes that arrived.
func rewriteBody(policy *plane.Policy, route string, body []byte) ([]byte, error) {
	body, err := withoutMediaDecodeKwargs(route, body)
	if err != nil {
		return nil, err
	}
	if policy == nil || len(policy.Rules) == 0 {
		return body, nil
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil || fields == nil {
		// A multipart body, or one that is not a JSON object. Both are left
		// exactly as they arrived, for the reason checkPolicy states.
		return body, nil
	}

	rules := inScope(policy.Rules, route, fields)
	// A copy reads the request as well as writing it, so every source is read out
	// of the body as it arrived, before any term has written into it. Same reason
	// the scopes are decided once: a rule carrying a key that another rule drops,
	// or that a third writes, would otherwise pick up a different value depending
	// on which of them ran first, and the order of the list is not part of this
	// language.
	sources := carried(rules, fields)

	written := false
	for _, rule := range rules {
		if rule.Key == "" {
			continue
		}
		var changed bool
		var err error
		switch {
		case rule.Drop:
			changed, err = drop(fields, rule.Key)
		case rule.ValueFrom != "":
			value, sent := sources[rule.ValueFrom]
			if !sent {
				// Nothing to carry. A rule whose source the request does not
				// have writes nothing, rather than writing a null the caller
				// never asked for.
				continue
			}
			changed, err = write(fields, rule.Key, value)
		case rule.Default != nil:
			changed, err = fill(fields, rule.Key, rule.Default)
		default:
			continue
		}
		if err != nil {
			return nil, err
		}
		written = written || changed
	}
	if !written {
		return body, nil
	}
	return json.Marshal(fields)
}

// chatCompletionsRoute is the one route that carries media by value, and so the
// only one the strip below has anything to say about.
const chatCompletionsRoute = "/v1/chat/completions"

// mediaDecodeKwargs is the request field that reconfigures the engine's media
// loader — `--media-io-kwargs` spelled per request.
const mediaDecodeKwargs = "media_io_kwargs"

// withoutMediaDecodeKwargs removes the caller's media decode settings from a
// chat completion body, and returns the exact bytes that arrived when there
// were none.
//
// It is not a policy term and it is not a refusal. Every bound on video decode
// lives in the serving recipe and in the worker — frame rate, per-frame size,
// decoded bytes, fetch bytes (docs/decisions/0017) — and this field is the one
// API surface that sets the same knobs from outside. Leaving it to a rule an
// endpoint opts into would make those bounds true only where somebody
// remembered to write the rule; leaving it to the caller would make them true
// until the first request that says otherwise.
//
// Dropped silently rather than refused. The field is a tuning hint on a request
// that is otherwise fine, and a 400 would break a client whose own defaults
// happen to spell out what we already do. What they asked for is not what they
// get, and the answer is still the answer to their question.
//
// `extra_body` is stripped as well. An OpenAI client merges that object into the
// top level before it sends, so the nesting only reaches this hop when a caller
// hand-wrote it — which the engine ignores today, and would stop ignoring the
// day anything on the path learns to unwrap it. The object itself stays, empty
// or not, for the reason `drop` leaves a parent behind.
func withoutMediaDecodeKwargs(route string, body []byte) ([]byte, error) {
	if route != chatCompletionsRoute {
		return body, nil
	}
	fields, readable := jsonObject(body)
	if !readable {
		return body, nil
	}

	_, carried := fields[mediaDecodeKwargs]
	nested, nestedReadable := jsonObject(fields["extra_body"])
	_, carriedNested := nested[mediaDecodeKwargs]
	if !carried && !(nestedReadable && carriedNested) {
		return body, nil
	}

	delete(fields, mediaDecodeKwargs)
	if nestedReadable && carriedNested {
		delete(nested, mediaDecodeKwargs)
		encoded, err := json.Marshal(nested)
		if err != nil {
			return nil, err
		}
		fields["extra_body"] = encoded
	}
	return json.Marshal(fields)
}

// fill writes value at path when the path's parent container is already in the
// request and carries nothing there, and reports whether it wrote anything.
//
// The parent must be present and it is never created. `thinking.type` fills a
// `thinking` object the caller sent without a `type`; it does nothing to a
// request that never mentioned thinking, because inventing the parent would
// turn a request that asked for no thinking at all into one that asked for the
// default — a change to what every caller of that endpoint gets, made on their
// behalf by a rule that was only supposed to spell out what they already meant.
//
// A key that is present is left alone even when it is null. The caller wrote
// something there, and a default is for what they left out.
func fill(fields map[string]json.RawMessage, path string, value any) (bool, error) {
	names := strings.Split(path, ".")
	containers, reachable := containersTo(fields, names)
	if !reachable {
		return false, nil
	}

	parent, leaf := containers[len(containers)-1], names[len(names)-1]
	if _, sent := parent[leaf]; sent {
		return false, nil
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		return false, err
	}
	parent[leaf] = encoded
	return true, reseal(containers, names)
}

// carried reads every ValueFrom source out of the request, once, before the
// rewrite starts. The map is keyed by the source path, so two rules copying the
// same field read the same bytes.
//
// A source that is not in the request is simply absent here, which is how a copy
// stays conditional without a `when_present` beside it saying the same thing
// twice.
func carried(rules []plane.Rule, fields map[string]json.RawMessage) map[string]json.RawMessage {
	var sources map[string]json.RawMessage
	for _, rule := range rules {
		if rule.Key == "" || rule.ValueFrom == "" {
			continue
		}
		value, sent := lookupRaw(fields, rule.ValueFrom)
		if !sent {
			continue
		}
		if sources == nil {
			sources = make(map[string]json.RawMessage, len(rules))
		}
		sources[rule.ValueFrom] = value
	}
	return sources
}

// write puts value at path, overwriting whatever is there, and reports whether
// the request actually changed.
//
// The parent must already be in the request and is never created — the same
// restraint fill keeps, for the same reason. What it does not keep is fill's
// deference to a value the caller wrote: this is the term for a field the
// provider ignores, and leaving the caller's value in place is exactly the wrong
// answer.
//
// A write of the value that is already there reports false, so a request nothing
// moved in is still forwarded as the exact bytes that arrived.
func write(fields map[string]json.RawMessage, path string, value json.RawMessage) (bool, error) {
	names := strings.Split(path, ".")
	containers, reachable := containersTo(fields, names)
	if !reachable {
		return false, nil
	}

	parent, leaf := containers[len(containers)-1], names[len(names)-1]
	if existing, sent := parent[leaf]; sent && string(existing) == string(value) {
		return false, nil
	}
	parent[leaf] = value
	return true, reseal(containers, names)
}

// drop removes the key at path, and reports whether there was one to remove.
//
// The parent it came out of stays, even when it is left empty: `{"thinking":{}}`
// and no `thinking` at all are two different requests, and a rewrite that turned
// one into the other would be answering a question nobody asked it.
func drop(fields map[string]json.RawMessage, path string) (bool, error) {
	names := strings.Split(path, ".")
	containers, reachable := containersTo(fields, names)
	if !reachable {
		return false, nil
	}

	parent, leaf := containers[len(containers)-1], names[len(names)-1]
	if _, sent := parent[leaf]; !sent {
		return false, nil
	}
	delete(parent, leaf)
	return true, reseal(containers, names)
}

// containersTo decodes the objects a path runs through, one level each,
// innermost last. It reports false when the path's parent is not in the request
// or is not an object — the two cases where there is nothing to write into and
// nothing to take out of.
//
// Only the containers on the path are decoded. Everything beside them —
// `messages`, `tools`, the whole conversation — stays raw bytes and is copied
// through, which is the same trade withModel makes and for the same reason.
func containersTo(fields map[string]json.RawMessage, names []string) ([]map[string]json.RawMessage, bool) {
	containers := make([]map[string]json.RawMessage, 1, len(names))
	containers[0] = fields
	for _, name := range names[:len(names)-1] {
		raw, sent := containers[len(containers)-1][name]
		if !sent {
			return nil, false
		}
		object, readable := jsonObject(raw)
		if !readable {
			return nil, false
		}
		containers = append(containers, object)
	}
	return containers, true
}

// reseal re-encodes each container into the one above it, innermost first, so a
// change to a nested object reaches the root map the caller marshals.
func reseal(containers []map[string]json.RawMessage, names []string) error {
	for depth := len(containers) - 1; depth > 0; depth-- {
		encoded, err := json.Marshal(containers[depth])
		if err != nil {
			return err
		}
		containers[depth-1][names[depth-1]] = encoded
	}
	return nil
}

// inScope returns the rules that apply to this request, ordered so that the
// violation reported for a body that breaks two of them is always the same one.
//
// Every scope term is optional and an omitted one asks nothing: no route matches
// every path, a nil stream matches both modes, and no `when_present` matches
// every body. So an unscoped policy behaves exactly as one written before scopes
// existed.
//
// The order is derived from each rule rather than taken from the array, because
// the array's order is not part of the language: a policy that means one thing
// and a re-serialisation of it that means another is a policy nobody can store.
func inScope(rules []plane.Rule, route string, fields map[string]json.RawMessage) []plane.Rule {
	type scoped struct {
		rule  plane.Rule
		order string
	}

	matched := make([]scoped, 0, len(rules))
	for _, rule := range rules {
		if rule.Route != "" && rule.Route != route {
			continue
		}
		if rule.Stream != nil {
			streaming, readable := streamOf(fields)
			if !readable || streaming != *rule.Stream {
				continue
			}
		}
		if rule.WhenPresent != "" {
			if _, present := lookup(fields, rule.WhenPresent); !present {
				continue
			}
		}
		matched = append(matched, scoped{rule: rule, order: ruleOrder(rule)})
	}
	sort.Slice(matched, func(i, j int) bool { return matched[i].order < matched[j].order })

	inScope := make([]plane.Rule, len(matched))
	for i, m := range matched {
		inScope[i] = m.rule
	}
	return inScope
}

// ruleOrder is a rule's place in the evaluation order, derived from the rule
// itself.
//
// Keys sort before predicates, then by subject, then by the rule's own
// encoding — which makes the order total without ever consulting the array's
// index, so two policies that differ only in how their rules were written down
// report the same violation. Keys first keeps the answer a request already got
// before scopes existed, when the parameter map was walked ahead of the denial
// list.
func ruleOrder(rule plane.Rule) string {
	kind, subject := "0", rule.Key
	if rule.Key == "" {
		kind, subject = "1", rule.Predicate
	}
	encoded, err := json.Marshal(rule)
	if err != nil {
		encoded = nil
	}
	return kind + "\x00" + subject + "\x00" + string(encoded)
}

// streamOf reads the request's streaming mode, reporting separately whether it
// could be read at all.
//
// Absent is a positive answer and it is false: `stream` defaults to off on every
// wire this gateway fronts, so a caller who sends nothing is asking for one
// response. A `stream` that is present but is not a boolean is not readable, and
// a rule scoped to a streaming mode does not apply to a request whose mode this
// hop cannot name — the upstream will answer for the field.
func streamOf(fields map[string]json.RawMessage) (streaming bool, readable bool) {
	raw, sent := fields["stream"]
	if !sent {
		return false, true
	}
	var value bool
	if err := json.Unmarshal(raw, &value); err != nil {
		return false, false
	}
	return value, true
}

// lookup reads the value at a dotted JSON path out of an already-parsed body.
//
// Absent and unreadable answer alike, because both mean the same thing to every
// caller here: there is no value to judge. A path whose parent is a string, an
// array or null does not resolve, and a rule about it constrains nothing rather
// than refusing a body it could not read.
//
// Only objects are walked. An index into an array is not a path this language
// has, and inventing one would put an expression where the design has data.
func lookup(fields map[string]json.RawMessage, path string) (any, bool) {
	raw, sent := lookupRaw(fields, path)
	if !sent {
		return nil, false
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return nil, false
	}
	return value, true
}

// lookupRaw is lookup without the decode: the bytes at a path, exactly as they
// arrived.
//
// Split out because a copy has no business decoding what it carries. `lookup`
// answers "what is this value" for a rule that has to compare it against an
// enum or a bound; `write` only has to put the same JSON somewhere else, and
// decoding it to `any` and re-encoding it would turn a number the caller wrote
// as `1` into `1` via float64 — a round trip with nothing to gain and a
// precision loss to lose.
func lookupRaw(fields map[string]json.RawMessage, path string) (json.RawMessage, bool) {
	names := strings.Split(path, ".")

	raw, sent := fields[names[0]]
	if !sent {
		return nil, false
	}
	for _, name := range names[1:] {
		object, readable := jsonObject(raw)
		if !readable {
			return nil, false
		}
		if raw, sent = object[name]; !sent {
			return nil, false
		}
	}
	return raw, true
}

// allowed reports whether one value satisfies one rule's value terms. A rule
// that states none permits everything, which is what an absent constraint has
// to mean.
func allowed(rule plane.Rule, value any) bool {
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

// mediaParts are the content-part shapes that carry media by value, and where
// the bytes sit inside each one.
//
// `image_url` is deliberately absent. The frontend decodes an image itself and
// already answers 400 for one it cannot read, so a rule here would be a second
// opinion about a question that is already answered correctly — and the format
// list below would be the thing deciding it, which is worse than the decoder.
// Video and audio are here because their bytes are not decoded until the worker,
// where a failure is a Python exception with no status on it and the frontend
// has nothing to triage: `Failed to generate completions`, 500, on a request
// that was the caller's mistake.
//
// `url` carries a data URL; `data` carries bare base64 with the container named
// in a sibling `format`. Two spellings because the wire has two: `video_url`
// follows the `image_url` shape the compatible servers settled on, and
// `input_audio` follows OpenAI's own audio part.
var mediaParts = map[string]string{
	"video_url":   "url",
	"audio_url":   "url",
	"input_audio": "data",
}

// mediaSignatures are the leading bytes of a container this rule can recognise,
// as (offset, magic) pairs.
//
// One table for audio and video rather than one each, and no branch on which
// part carried the bytes. A container is not a modality: audio arrives inside
// MP4 and Ogg as often as video does, and a rule that demanded a video magic
// under `video_url` would refuse an audio-only MP4 that the engine plays.
var mediaSignatures = []struct {
	offset int
	magic  string
}{
	{4, "ftyp"},             // ISO BMFF — mp4, m4a, mov, 3gp
	{0, "\x1a\x45\xdf\xa3"}, // EBML — Matroska, WebM
	{0, "OggS"},             // Ogg — Vorbis, Opus, Theora
	{0, "RIFF"},             // RIFF — wav, avi
	{0, "fLaC"},
	{0, "FLV\x01"},
	{0, "ID3"},      // mp3 with a tag
	{0, "\xff\xfb"}, // mp3 without one, the common layer III frame headers
	{0, "\xff\xf3"},
	{0, "\xff\xf2"},
	{0, "#!AMR"},
	{0, "FORM"},             // AIFF
	{0, "\x00\x00\x01\xba"}, // MPEG program stream
	{0, "\x00\x00\x01\xb3"}, // MPEG video elementary stream
}

// transportStreamSync and transportStreamPacket are the MPEG-TS sync byte and
// the stride it repeats on.
//
// TS is not in mediaSignatures because its signature is one byte, and one byte
// admits one payload in every 256 whatever the payload is — an allowlist with a
// hole that size in it is not one. Two syncs a packet apart is the cheapest test
// that actually distinguishes a transport stream from a coincidence.
const (
	transportStreamSync   = 0x47
	transportStreamPacket = 188
)

// shortestMedia is the fewest bytes this rule will look at before calling a
// payload truncated.
//
// It is the longest offset+magic above rather than a round number, so the
// question "could these bytes match anything we know" has one answer: below this
// length no signature can be checked, and refusing is not a guess about the
// format but a statement about the length.
const shortestMedia = 8

// undecodableMessageMedia refuses a media content part whose bytes cannot be a
// container, before the request reaches an engine that would answer 500 for it.
//
// This is the narrowest form of the rule that is still worth having. Three
// things are refused — base64 that does not decode, an empty payload, and a
// payload too short or too unlike any container this build knows — and nothing
// else. In particular a well-formed container the decoder still rejects is not
// refused here: the engine is the thing that knows whether a stream inside a
// valid MP4 is playable, and this rule has no standing to pre-empt it.
//
// A remote URL is never touched. Fetching an address a caller supplied is an
// egress decision, and this rule reads the request it was given.
//
// The signature list is an allowlist, which is the one place this rule can be
// wrong in the direction that costs a customer a request: a container nobody
// here has heard of is refused with a 400 where the engine might have served it.
// That is the trade the list is, and it is why the list is generous, why the
// error names the length or the encoding whenever that is the actual fault, and
// why `image_url` — the one modality that already gets a correct answer — is
// not in `mediaParts`.
func undecodableMessageMedia(fields map[string]json.RawMessage) (string, string, bool) {
	messages, readable := jsonArray(fields["messages"])
	if !readable {
		return "", "", false
	}

	for index, raw := range messages {
		message, readable := jsonObject(raw)
		if !readable {
			continue
		}
		parts, isArray := jsonArray(message["content"])
		if !isArray {
			// A string content carries no media, and a content this rule cannot
			// read is one the upstream will answer for.
			continue
		}
		for position, raw := range parts {
			part, readable := jsonObject(raw)
			if !readable {
				continue
			}
			kind, stated := jsonString(part["type"])
			if !stated {
				continue
			}
			field, carriesMedia := mediaParts[kind]
			if !carriesMedia {
				continue
			}
			payload, stated := jsonString(objectRaw(part[kind], field))
			if !stated || payload == "" {
				// An unstated payload is a shape fault, not an encoding one, and
				// the upstream's own words for it are better than a guess here.
				continue
			}
			encoded, byValue := mediaBase64(field, payload)
			if !byValue {
				// A remote reference, or a data URL that is not base64. Neither
				// is bytes this rule was given.
				continue
			}
			if fault := mediaFault(encoded); fault != "" {
				return refusal("The %s in messages[%d].content[%d] %s.", kind, index, position, fault)
			}
		}
	}

	return "", "", false
}

// mediaBase64 returns the base64 payload a content part carries by value, and
// whether it carried one at all.
//
// The field name decides how to read it, which is why it is carried in
// `mediaParts` rather than assumed. A `data` field is already the encoding —
// `input_audio` names its container in a sibling `format` and needs no URL. A
// `url` field is base64 only when the data URL says `;base64`: the
// percent-encoded form is legal and this rule cannot judge it, so it reports no
// payload rather than decoding the wrong thing. An `http(s)` URL is a reference
// and reports the same.
func mediaBase64(field, payload string) (string, bool) {
	if field == "data" {
		return payload, true
	}
	if !strings.HasPrefix(payload, "data:") {
		return "", false
	}
	comma := strings.IndexByte(payload, ',')
	if comma < 0 {
		// `data:` with no comma is not a data URL at all. Left to the upstream,
		// which will say so about the whole part rather than about its bytes.
		return "", false
	}
	if !strings.Contains(payload[:comma], ";base64") {
		return "", false
	}
	return payload[comma+1:], true
}

// mediaFault names what is wrong with a payload, or returns empty when nothing
// this rule can see is.
//
// The three faults are worded so a caller can act on each: which of the encoding
// and the bytes is wrong, and if the bytes, whether there were too few of them
// to be anything or enough to be something this build does not know.
func mediaFault(encoded string) string {
	trimmed := strings.TrimSpace(encoded)
	decoded, err := base64.StdEncoding.DecodeString(trimmed)
	if err != nil {
		// Both alphabets, because a caller who built the payload URL-safe is
		// making a portability mistake and not sending a corrupt file. Both are
		// the padded encodings: the engine decodes with Python's `b64decode`,
		// which requires the padding, so accepting an unpadded payload here
		// would forward a request this rule had declared sound and the worker
		// then answers 500 for — which is the whole defect, one layer along.
		decoded, err = base64.URLEncoding.DecodeString(trimmed)
	}
	if err != nil {
		return "is not valid base64"
	}
	if len(decoded) == 0 {
		return "decodes to no bytes"
	}
	if len(decoded) < shortestMedia {
		return fmt.Sprintf("decodes to %d bytes, which is too few to be a media container — the payload looks truncated", len(decoded))
	}
	if !hasMediaSignature(decoded) {
		return "does not decode to a media container this endpoint recognises"
	}
	return ""
}

// hasMediaSignature reports whether the bytes begin like a container in
// mediaSignatures, or like a transport stream.
func hasMediaSignature(decoded []byte) bool {
	if isTransportStream(decoded) {
		return true
	}
	for _, signature := range mediaSignatures {
		end := signature.offset + len(signature.magic)
		if len(decoded) < end {
			continue
		}
		if string(decoded[signature.offset:end]) == signature.magic {
			return true
		}
	}
	return false
}

// objectRaw reads one field out of a JSON object without decoding its value.
//
// objectField's counterpart for a field whose value is read as JSON rather than
// compared as a Go value. It answers with an empty RawMessage for an object it
// cannot read, so a caller that then asks for a string gets "not stated" — which
// is what an unreadable container means to every caller here.
func objectRaw(raw json.RawMessage, name string) json.RawMessage {
	object, readable := jsonObject(raw)
	if !readable {
		return nil
	}
	return object[name]
}

// isTransportStream reports two MPEG-TS sync bytes one packet apart.
//
// A payload shorter than a packet and a half cannot be tested this way and is
// not a transport stream as far as this rule is concerned. That is the same
// answer the length test gives and for the same reason: below the length at
// which a format is identifiable, refusing is a statement about the bytes there
// are and not a guess about the ones there are not.
func isTransportStream(decoded []byte) bool {
	if len(decoded) <= transportStreamPacket {
		return false
	}
	return decoded[0] == transportStreamSync && decoded[transportStreamPacket] == transportStreamSync
}
