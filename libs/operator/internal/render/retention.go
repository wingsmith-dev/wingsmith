package render

import (
	"strconv"
	"strings"
)

// verboseLevels are the log levels at which some engine on the serving path writes request content:
// Dynamo's frontend logs every request whole at trace, vLLM's tool parsers log streamed completion
// text at debug, and so do SGLang's tool-call detectors. `DYN_LOG=debug` is mapped onto vLLM's and
// SGLang's Python loggers by Dynamo, so one variable reaches all three. The plane refuses the same
// set (`VERBOSE_LEVELS` in `libs/plane/app/core/serving.py`); this takes it back out of any row
// written before it did.
var verboseLevels = map[string]bool{"debug": true, "trace": true, "notset": true}

// logLevelFlags are engine flags that set a log level, removed when they set a verbose one.
var logLevelFlags = []string{"log-level"}

// isVerbose reports whether a log level, by name or by Python's number, logs request content.
func isVerbose(level string) bool {
	value := strings.ToLower(strings.TrimSpace(level))
	if n, err := strconv.Atoi(value); err == nil {
		return n < 20
	}
	return verboseLevels[value]
}

func dynLogIsVerbose(value string) bool {
	for _, directive := range strings.Split(value, ",") {
		if strings.TrimSpace(directive) == "" {
			continue
		}
		level := directive
		if i := strings.LastIndex(directive, "="); i >= 0 {
			level = directive[i+1:]
		}
		if isVerbose(level) {
			return true
		}
	}
	return false
}

func switchedOn(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "", "0", "false", "no", "off":
		return false
	}
	return true
}

// contentLoggingEnv maps each environment variable that can make a worker or the frontend log
// request content to the values that do. `DYN_LOGGING_CONFIG_PATH` names a file of tracing filters
// this cannot read, so any value is one.
var contentLoggingEnv = map[string]func(string) bool{
	"DYN_LOG":                            dynLogIsVerbose,
	"DYN_LOGGING_CONFIG_PATH":            func(string) bool { return true },
	"VLLM_LOGGING_LEVEL":                 isVerbose,
	"VLLM_DEBUG_LOG_API_SERVER_RESPONSE": switchedOn,
}

// withoutContentLoggingEnv drops every environment entry that would log request content from the
// rendered graph, wherever it sits: a pod's `env`, a Dynamo component's `envs`, the frontend's.
//
// It runs after the overlays are merged, because the overlays are the only way one of these
// variables arrives and they are merged last. Dropping the entry is the clamp: every one of them
// defaults to info, or off. An entry set through `valueFrom` cannot be read here and is dropped as
// well. Any `{name, value}` map in any list is read as an environment entry; nothing else in a
// DynamoGraphDeployment has that shape with one of these names.
func withoutContentLoggingEnv(node any) any {
	switch value := node.(type) {
	case map[string]any:
		out := make(map[string]any, len(value))
		for key, child := range value {
			out[key] = withoutContentLoggingEnv(child)
		}
		return out
	case []any:
		// A new slice, not value[:0]: a list the Dynamo overlay supplied is shared with the inputs.
		kept := make([]any, 0, len(value))
		for _, item := range value {
			if logsContent(item) {
				continue
			}
			kept = append(kept, withoutContentLoggingEnv(item))
		}
		return kept
	}
	return node
}

func logsContent(item any) bool {
	entry, ok := item.(map[string]any)
	if !ok {
		return false
	}
	name, _ := entry["name"].(string)
	verbose, watched := contentLoggingEnv[name]
	if !watched {
		return false
	}
	if _, ok := entry["valueFrom"]; ok {
		return true
	}
	if v, ok := entry["value"].(string); ok {
		return verbose(v)
	}
	return false
}
