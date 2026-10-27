package serving

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

// denials are the wire features a policy may refuse by name.
//
// These and `diagnoses` are the gateway's built-in predicates. None applies by
// default: a plane policy selects one by name for an endpoint whose terms call
// for it. Each reproduces a refusal measured against a hosted provider's API;
// names are JSON paths, not provider names, and which endpoint carries which
// name is the plane's business.
//
// A predicate reads the whole body and refuses only what it can positively
// identify. A shape it does not recognise is left to the upstream.
var denials = map[string]func(map[string]json.RawMessage) bool{
	// The reference provider answers 400 "tool_choice 'specified' is
	// incompatible with thinking enabled" in exactly one cell:
	//
	//	tool_choice                     thinking on   thinking off
	//	auto / none / required          200           200
	//	{"type":"function",...}         400           200
	//	{"type":"allowed_tools",...}    200           200
	"tool_choice.named_while_thinking": func(fields map[string]json.RawMessage) bool {
		return isNamedFunctionChoice(fields["tool_choice"]) && thinkingEnabled(fields)
	},
}

// diagnoses are predicates that answer in their own words: a refusal about the
// request (which declaration, what is wrong) instead of about the endpoint, so
// the caller has something to correct. They are looked up in the same pass as
// `denials` and keep the same promises: an unknown name is ignored, and a shape
// a rule cannot read is left to the upstream.
var diagnoses = map[string]func(map[string]json.RawMessage) (code string, message string, refused bool){
	// The `tools` on an element of `messages`. The request-level `tools` array
	// is read only to compare names, never judged.
	"messages.tools": malformedMessageTools,
	// The media content parts on an element of `messages`.
	"messages.media": undecodableMessageMedia,
	// Answers in its own words because the accepted value depends on the mode.
	"temperature.not_pinned_for_mode": temperatureOffProviderPin,
}

// temperaturePins are the only temperature the provider accepts, per thinking
// mode, measured against its hosted API; every other value is a 400:
//
//	temperature   thinking on   thinking off
//	1.0           200           400
//	0.6           400           200
//	0.0           400           400
//
// The provider's published conformance table lists 0.6 and 0.0 as accepted with
// thinking on, and the provider refuses both. That is why this is a term an
// endpoint opts into: which of the two an endpoint is sold against is a
// commercial decision.
var temperaturePins = map[bool]float64{true: 1, false: 0.6}

// temperatureOffProviderPin refuses a temperature the provider would refuse in
// the mode this request is in.
//
// An absent `temperature` is never a violation. A present but unreadable one,
// `null` included (it decodes to zero without error), is left to the upstream.
// A malformed `thinking` makes the rule abstain: the mode chooses the pinned
// value, and guessing it would refuse a value that may be right.
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

// isNamedFunctionChoice reports the `{"type":"function","function":{...}}` form,
// as distinct from `allowed_tools` (also an object) and the bare strings.
func isNamedFunctionChoice(raw json.RawMessage) bool {
	value, _ := objectField(raw, "type")
	return value == "function"
}

// thinkingEnabled reports whether this request positively reaches the model
// thinking. Absent means enabled: the model thinks unless told not to.
//
// Only `thinking.type` is read. The provider ignores
// `chat_template_kwargs.thinking` and thinks anyway, so a policy that wants the
// engine to agree removes that field with a `drop` term.
//
// A present but unreadable field answers false, so the request is not refused
// here and the upstream reports the malformed field in its own words.
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
// whether the object could be read at all, so a malformed body stays
// distinguishable from one that simply lacks the field.
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

// maxToolNameLength is the longest function name the provider takes. Not
// OpenAI's 64: narrowing it would refuse names the provider serves.
const maxToolNameLength = 256

// malformedMessageTools reports a tool declared on a message in a shape the
// provider answers 400 to, and says which declaration and why. The engine drops
// message-level tools silently, so without this a malformed one gets a 200 with
// the tool missing.
//
// Measured against the reference provider's hosted API. Refused:
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
// Served: a declaration on any system position, several tools on one message,
// several messages carrying tools, and a message-level name beside a different
// request-level one. "@" stands for any character outside `[A-Za-z0-9_-]` (the
// OpenAI charset), and null for any item that is not an object. `parameters` is
// not required, so a tool with no arguments passes.
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
			// An explicit null is no declaration: many clients serialise an
			// absent optional that way.
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
			// Only string content is judged; an array of parts was never
			// measured.
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

// refusal builds a diagnosis's answer, with the code this package already uses
// for a value it will not take.
func refusal(format string, args ...any) (string, string, bool) {
	return "invalid_parameter", fmt.Sprintf(format, args...), true
}

// toolDeclaration reads one declaration, returning either its name or the
// sentence that says what is wrong with it, never both.
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

// isToolName reports a name the provider will take, length aside. A digit is
// allowed anywhere but first, as the provider does.
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

// requestToolNames collects the names the request-level `tools` array declares,
// so a message-level declaration can be checked against them. It refuses
// nothing: that array is the upstream's to judge.
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

// jsonArray, jsonObject, jsonString and isJSONNull each report whether the
// value had the needed shape separately from its contents, as objectField does.
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

// jsonObject treats null as unreadable: it decodes into a nil map without
// error, and `tools: [null]` must not read as a tool missing its fields.
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
// this route, or ok when there is none. A nil policy costs one nil check.
//
// Every rule in scope applies; there is no precedence, and contradictory rules
// are refused where the policy is written. An absent parameter is never a
// violation: a provider pins the value a caller sends, it does not demand the
// field.
func checkPolicy(policy *plane.Policy, route string, body []byte) (code string, message string, ok bool) {
	if policy == nil || len(policy.Rules) == 0 {
		return "", "", true
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil || fields == nil {
		// Not this check's business: a JSON body that fails here was
		// reported where its `model` was read. A multipart body passes
		// unchecked, since policies are written against the JSON wire.
		return "", "", true
	}

	for _, rule := range inScope(policy.Rules, route, fields) {
		switch {
		case rule.Predicate != "":
			if !rule.Deny {
				// A predicate rule that does not deny constrains nothing; the
				// plane refuses it at author time.
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
			// Unknown to this build: reported at refresh time, never a
			// reason to drop the request.

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
// declare applied, and the body untouched when there is nothing to write. It
// never refuses; checkPolicy does that.
//
// Which rules apply is decided once against the body as it arrived, so the pass
// is one simultaneous edit and the order of the rule list does not matter. The
// body is re-encoded only when something was written.
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
	// Sources are read from the body as it arrived, before any term writes,
	// for the same reason the scopes are decided once.
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
				// No source in the request: write nothing, not a null.
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
// loader: `--media-io-kwargs` spelled per request.
const mediaDecodeKwargs = "media_io_kwargs"

// withoutMediaDecodeKwargs removes the caller's media decode settings from a
// chat completion body, and returns the exact bytes that arrived when there
// were none.
//
// Not a policy term: every bound on video decode lives in the serving recipe
// and the worker (docs/decisions/0017), and this field is the one API surface
// that sets those knobs from outside. Dropped silently, since a 400 would break
// clients whose defaults spell out what we already do. A hand-written
// `extra_body` copy is stripped too; the object itself stays, as with `drop`.
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

// fill writes value at path when the path's parent is already in the request
// and carries nothing there, and reports whether it wrote anything. The parent
// is never created: filling `thinking.type` must not turn a request that never
// mentioned thinking into one that asked for the default. A key present as null
// is left alone.
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

// carried reads every ValueFrom source out of the request once, before the
// rewrite starts, keyed by source path. An absent source is absent here, which
// keeps a copy conditional without a `when_present`.
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
// the request changed. Like fill it never creates the parent; unlike fill it
// replaces the caller's value, since this is the term for a field the provider
// ignores. Writing the value already there reports false.
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

// drop removes the key at path, and reports whether there was one. The parent
// stays even when left empty: `{"thinking":{}}` and no `thinking` differ.
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

// containersTo decodes the objects a path runs through, innermost last, and
// reports false when the parent is missing or not an object. Everything off
// the path stays raw bytes, as in withModel.
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

// inScope returns the rules that apply to this request, in an order derived
// from each rule (ruleOrder) so a body breaking two rules always reports the
// same one. An omitted scope term matches everything.
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

// ruleOrder is a rule's place in the evaluation order: keys before predicates,
// then by subject, then by the rule's encoding. It never uses the array index,
// so two encodings of one policy report the same violation.
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

// streamOf reads the request's streaming mode and whether it could be read.
// Absent is readable and false; a non-boolean is unreadable, and a rule scoped
// to a mode does not apply to it.
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

// lookup reads the value at a dotted JSON path out of a parsed body. Absent and
// unreadable answer alike: there is no value to judge. Only objects are walked;
// there is no array indexing.
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

// lookupRaw is lookup without the decode: the bytes at a path as they arrived,
// so a copy does not round-trip numbers through float64.
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

// inEnum compares against the permitted set. Only JSON scalars can match; an
// object or array is never equal, and == on one would panic.
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

// mediaParts are the content-part shapes that carry media by value, and the
// field that holds the bytes: `url` holds a data URL, `data` bare base64.
//
// `image_url` is absent: the frontend decodes images itself and answers 400 for
// a bad one. Video and audio are only decoded in the worker, where a bad
// payload becomes a 500 with nothing for the frontend to triage.
var mediaParts = map[string]string{
	"video_url":   "url",
	"audio_url":   "url",
	"input_audio": "data",
}

// mediaSignatures are the leading bytes of a container this rule recognises, as
// (offset, magic) pairs. One table for audio and video, since a container is
// not a modality: audio arrives in MP4 and Ogg as often as video does.
var mediaSignatures = []struct {
	offset int
	magic  string
}{
	{4, "ftyp"},             // ISO BMFF: mp4, m4a, mov, 3gp
	{0, "\x1a\x45\xdf\xa3"}, // EBML: Matroska, WebM
	{0, "OggS"},             // Ogg: Vorbis, Opus, Theora
	{0, "RIFF"},             // RIFF: wav, avi
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
// its stride. TS is tested as two syncs a packet apart, since a one-byte
// signature would match one payload in 256.
const (
	transportStreamSync   = 0x47
	transportStreamPacket = 188
)

// shortestMedia is the fewest bytes this rule looks at before calling a payload
// truncated: the longest offset+magic in mediaSignatures.
const shortestMedia = 8

// undecodableMessageMedia refuses a media content part whose bytes cannot be a
// container, before the request reaches an engine that would answer 500.
//
// It refuses base64 that does not decode, an empty payload, and a payload too
// short or unlike any known container, and nothing else: whether a valid
// container is playable is the engine's call. Remote URLs are never fetched.
// The signature list is an allowlist, so an unknown container is refused; the
// list is generous for that reason.
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
// whether it carried one. A `data` field is the encoding itself; a `url` is
// base64 only as a `;base64` data URL. A percent-encoded data URL or an http(s)
// reference reports no payload.
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
		// which will say so about the whole part, not about its bytes.
		return "", false
	}
	if !strings.Contains(payload[:comma], ";base64") {
		return "", false
	}
	return payload[comma+1:], true
}

// mediaFault names what is wrong with a payload, or returns empty when nothing
// this rule can see is, worded so the caller knows which part to fix.
func mediaFault(encoded string) string {
	trimmed := strings.TrimSpace(encoded)
	decoded, err := base64.StdEncoding.DecodeString(trimmed)
	if err != nil {
		// Both alphabets, padded only: the worker decodes with Python's
		// b64decode, which requires padding.
		decoded, err = base64.URLEncoding.DecodeString(trimmed)
	}
	if err != nil {
		return "is not valid base64"
	}
	if len(decoded) == 0 {
		return "decodes to no bytes"
	}
	if len(decoded) < shortestMedia {
		return fmt.Sprintf("decodes to %d bytes, which is too few to be a media container: the payload looks truncated", len(decoded))
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

// objectRaw reads one field out of a JSON object without decoding it, or nil
// when the object cannot be read.
func objectRaw(raw json.RawMessage, name string) json.RawMessage {
	object, readable := jsonObject(raw)
	if !readable {
		return nil
	}
	return object[name]
}

// isTransportStream reports two MPEG-TS sync bytes one packet apart. Anything
// shorter than a packet and one byte is not one.
func isTransportStream(decoded []byte) bool {
	if len(decoded) <= transportStreamPacket {
		return false
	}
	return decoded[0] == transportStreamSync && decoded[transportStreamPacket] == transportStreamSync
}
