// Package errs defines the typed error contract shared by every command.
package errs

import (
	"errors"
	"fmt"
	"sort"
	"strings"
)

// Code identifies an error class. Codes are stable API: agents and scripts
// branch on them, and each maps to a fixed process exit code.
type Code string

const (
	CodeOK Code = "OK"

	CodeUsage         Code = "USAGE"
	CodeInputRequired Code = "INPUT_REQUIRED"

	CodeAuthRequired Code = "AUTH_REQUIRED"
	CodeAuthExpired  Code = "AUTH_EXPIRED"
	CodeForbidden    Code = "FORBIDDEN"

	CodeNotFound  Code = "NOT_FOUND"
	CodeAmbiguous Code = "AMBIGUOUS"

	CodeConflict     Code = "CONFLICT"
	CodeInvalidState Code = "INVALID_STATE"

	CodeQuotaExceeded       Code = "QUOTA_EXCEEDED"
	CodeCapacityUnavailable Code = "CAPACITY_UNAVAILABLE"

	CodeTimeout     Code = "TIMEOUT"
	CodeRateLimited Code = "RATE_LIMITED"

	CodeServerError Code = "SERVER_ERROR"
	CodeNetwork     Code = "NETWORK"

	CodeConfirmationRequired   Code = "CONFIRMATION_REQUIRED"
	CodeInteractiveUnavailable Code = "INTERACTIVE_UNAVAILABLE"
	CodeUnimplemented          Code = "UNIMPLEMENTED"

	// CodeOperationFailed is an operation or benchmark that was waited on and
	// ended in anything but success: the request was fine, the work was not.
	CodeOperationFailed Code = "OPERATION_FAILED"

	CodeInterrupted Code = "INTERRUPTED"
	CodeInternal    Code = "INTERNAL"
)

var exitCodes = map[Code]int{
	CodeOK:                     0,
	CodeUsage:                  2,
	CodeInputRequired:          2,
	CodeAuthRequired:           3,
	CodeAuthExpired:            3,
	CodeForbidden:              3,
	CodeNotFound:               4,
	CodeConflict:               5,
	CodeInvalidState:           5,
	CodeAmbiguous:              5,
	CodeConfirmationRequired:   5,
	CodeInteractiveUnavailable: 5,
	CodeUnimplemented:          5,
	CodeQuotaExceeded:          6,
	CodeCapacityUnavailable:    7,
	CodeTimeout:                8,
	CodeRateLimited:            9,
	CodeServerError:            10,
	CodeNetwork:                10,
	CodeOperationFailed:        11,
	CodeInterrupted:            130,
	CodeInternal:               1,
}

// Action is a suggested follow-up: a human description plus a runnable command.
type Action struct {
	Description string `json:"description"`
	Command     string `json:"command,omitempty"`
}

// Error is the CLI's single error type. Every failure path produces one so
// that human, JSON, and agent renderings stay identical in substance.
type Error struct {
	Code        Code     `json:"code"`
	Message     string   `json:"message"`
	Cause       string   `json:"cause,omitempty"`
	Retryable   bool     `json:"retryable,omitempty"`
	RetryAfterS int      `json:"retry_after_s,omitempty"`
	Remediation []Action `json:"remediation,omitempty"`
	Docs        string   `json:"docs,omitempty"`
	RequiredFlg string   `json:"required_flag,omitempty"`

	wrapped error
}

func (e *Error) Error() string { return e.Message }
func (e *Error) Unwrap() error { return e.wrapped }

// ExitCode maps the error to a process exit status.
func (e *Error) ExitCode() int {
	if c, ok := exitCodes[e.Code]; ok {
		return c
	}
	return 1
}

// New builds an error with the given code and formatted message.
func New(code Code, format string, args ...any) *Error {
	return &Error{Code: code, Message: fmt.Sprintf(format, args...), Docs: docsURL(code)}
}

// Wrap adapts an arbitrary error into the typed contract.
func Wrap(err error, code Code, format string, args ...any) *Error {
	e := New(code, format, args...)
	e.wrapped = err
	if err != nil {
		e.Cause = err.Error()
	}
	return e
}

// WithCause attaches an explanation of why the failure happened.
func (e *Error) WithCause(cause string) *Error {
	e.Cause = cause
	return e
}

// WithCausef attaches a formatted explanation.
func (e *Error) WithCausef(format string, args ...any) *Error {
	e.Cause = fmt.Sprintf(format, args...)
	return e
}

// WithFix appends a remediation step.
func (e *Error) WithFix(description, command string) *Error {
	e.Remediation = append(e.Remediation, Action{Description: description, Command: command})
	return e
}

// WithRetry marks the error as worth retrying after the given delay.
func (e *Error) WithRetry(afterSeconds int) *Error {
	e.Retryable = true
	e.RetryAfterS = afterSeconds
	return e
}

// WithFlag records the flag that would have satisfied the request.
func (e *Error) WithFlag(flag string) *Error {
	e.RequiredFlg = flag
	return e
}

// As extracts a *Error from any error, synthesising one when absent.
func As(err error) *Error {
	if err == nil {
		return nil
	}
	var e *Error
	if errors.As(err, &e) {
		return e
	}
	return Wrap(err, CodeInternal, "%s", err.Error())
}

// DocsURL is where the error codes are documented. It is the CLI's own README
// in the repository; `hgr explain <code>` prints the same thing offline.
const DocsURL = "https://github.com/hangar-dev/hangar/blob/main/libs/cli/README.md#errors-and-exit-codes"

func docsURL(code Code) string {
	if code == CodeOK {
		return ""
	}
	return DocsURL
}

// Explanation is the long-form description surfaced by `hgr explain`.
type Explanation struct {
	Code     Code     `json:"code"`
	Exit     int      `json:"exit_code"`
	Summary  string   `json:"summary"`
	Detail   string   `json:"detail"`
	Fixes    []string `json:"common_fixes"`
	Retryabl bool     `json:"typically_retryable"`
}

var explanations = map[Code]Explanation{
	CodeUsage: {
		Summary: "The command was invoked incorrectly.",
		Detail:  "A required argument or flag was missing, or a value did not parse. Nothing was sent to the control plane.",
		Fixes:   []string{"Run the command with --help"},
	},
	CodeInputRequired: {
		Summary: "A required value was not supplied and prompting is disabled.",
		Fixes:   []string{"Pass the value as a flag", "Drop --no-input in an interactive shell"},
	},
	CodeAuthRequired: {
		Summary: "No credentials are available for the active context.",
		Detail:  "The CLI found no token in the config file, HANGAR_TOKEN, or --token.",
		Fixes:   []string{"hgr login", "hgr context ls"},
	},
	CodeAuthExpired: {
		Summary: "The session has expired and could not be renewed.",
		Detail:  "The access token expired and the plane refused the stored refresh token, or none was stored.",
		Fixes:   []string{"hgr login"},
	},
	CodeForbidden: {
		Summary: "The credentials are valid but lack permission.",
		Fixes:   []string{"hgr context", "Ask an operator for a role upgrade"},
	},
	CodeNotFound: {
		Summary: "The referenced object does not exist.",
		Detail:  "Names resolve within the active project. An object created in another project is invisible here.",
		Fixes:   []string{"hgr cluster ls", "hgr context"},
	},
	CodeAmbiguous: {
		Summary: "The identifier prefix matched more than one object.",
		Fixes:   []string{"Use the full ID or name"},
	},
	CodeConflict: {
		Summary: "The object already exists or is being mutated concurrently.",
		Fixes:   []string{"Pick a different name", "Retry the command"},
	},
	CodeInvalidState: {
		Summary: "The object is not in a state that allows this operation.",
		Detail:  "For example, a draining cluster cannot accept new deployments.",
		Fixes:   []string{"hgr cluster get <cluster>", "hgr operation ls"},
	},
	CodeQuotaExceeded: {
		Summary: "The request would exceed the tenant's declared quota.",
		Fixes:   []string{"hgr quota ls"},
	},
	CodeCapacityUnavailable: {
		Summary:  "No cluster has devices matching the workload's device shape.",
		Detail:   "Cluster selection is global and coarse: a device class unavailable in one region is often available in another.",
		Fixes:    []string{"hgr device ls", "hgr cluster ls"},
		Retryabl: true,
	},
	CodeTimeout: {
		Summary:  "The operation did not complete within the deadline.",
		Fixes:    []string{"Raise --timeout for one request, or --wait-timeout for a --wait", "Poll with `hgr operation get <id>`"},
		Retryabl: true,
	},
	CodeRateLimited: {
		Summary:  "Too many requests in a short window.",
		Fixes:    []string{"Back off and retry", "Batch calls with -o json instead of polling"},
		Retryabl: true,
	},
	CodeServerError: {
		Summary:  "The control plane returned an unexpected error.",
		Fixes:    []string{"Retry", "hgr version"},
		Retryabl: true,
	},
	CodeNetwork: {
		Summary:  "The control plane could not be reached.",
		Fixes:    []string{"Check --server and any proxy settings", "Run offline against the fake: hgr --fake …"},
		Retryabl: true,
	},
	CodeConfirmationRequired: {
		Summary: "A destructive operation was not confirmed.",
		Fixes:   []string{"Re-run with --yes"},
	},
	CodeInteractiveUnavailable: {
		Summary: "The command needs an interactive terminal.",
		Fixes:   []string{"Pass every value as a flag and add --no-input"},
	},
	CodeUnimplemented: {
		Summary: "The command is registered but not implemented yet.",
		Detail:  "The plane's API contract for this noun is still being written. Only `cluster` is wired end to end.",
		Fixes:   []string{"hgr cluster ls"},
	},
	CodeOperationFailed: {
		Summary: "An operation or benchmark that was waited on did not succeed.",
		Detail:  "The request was accepted and the work ran, but it ended failed or cancelled. The object carries the reason.",
		Fixes:   []string{"hgr operation get <id>", "hgr benchmark get <id>"},
	},
	CodeInterrupted: {
		Summary: "The command was interrupted.",
		Fixes:   []string{"hgr operation ls"},
	},
	CodeInternal: {
		Summary: "The CLI hit an unexpected condition.",
		Fixes:   []string{"Re-run with --debug"},
	},
}

// Explain returns the long-form description of a code.
func Explain(code Code) (Explanation, bool) {
	e, ok := explanations[code]
	if !ok {
		return Explanation{}, false
	}
	e.Code = code
	e.Exit = exitCodes[code]
	return e, true
}

// AllCodes lists every known code in sorted order.
func AllCodes() []Code {
	out := make([]Code, 0, len(explanations))
	for c := range explanations {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// ParseCode normalises user input into a Code.
func ParseCode(s string) Code {
	return Code(strings.ToUpper(strings.ReplaceAll(strings.TrimSpace(s), "-", "_")))
}
