package errs

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"testing"
)

// allCodes is every code the package declares, as opposed to every code that has an explanation.
// The two lists differ by design, and the tests below hold each to its own rule.
var allCodes = []Code{
	CodeOK,
	CodeUsage, CodeInputRequired,
	CodeAuthRequired, CodeAuthExpired, CodeForbidden,
	CodeNotFound, CodeAmbiguous,
	CodeConflict, CodeInvalidState,
	CodeTimeout, CodeRateLimited,
	CodeServerError, CodeNetwork, CodeOperationFailed,
	CodeConfirmationRequired,
	CodeInterrupted, CodeInternal,
}

// TestEveryCodeHasAnExitStatus. Codes are stable API (scripts branch on them, and each maps to a
// fixed process exit code), so a code added without one would silently exit 1 alongside a genuine
// internal error, which is the one thing a caller most needs to tell apart.
func TestEveryCodeHasAnExitStatus(t *testing.T) {
	for _, c := range allCodes {
		if _, ok := exitCodes[c]; !ok {
			t.Errorf("%s has no exit status and would fall through to 1", c)
		}
	}
	if len(exitCodes) != len(allCodes) {
		t.Errorf("exitCodes holds %d entries for %d codes; one list has moved without the other",
			len(exitCodes), len(allCodes))
	}
}

// TestSuccessIsTheOnlyZeroExit: anything else exiting 0 would make a failure look like a success
// to every `set -e` script that runs the CLI.
func TestSuccessIsTheOnlyZeroExit(t *testing.T) {
	for code, exit := range exitCodes {
		if exit == 0 && code != CodeOK {
			t.Errorf("%s exits 0; a failure would read as a success", code)
		}
	}
	if exitCodes[CodeOK] != 0 {
		t.Error("OK does not exit 0")
	}
}

// TestExitCodesStayWhereTheyAre. These numbers are what a caller's script branches on; changing
// one silently repurposes an exit status already in use somewhere.
func TestExitCodesStayWhereTheyAre(t *testing.T) {
	pinned := map[Code]int{
		CodeOK: 0, CodeInternal: 1, CodeUsage: 2, CodeAuthRequired: 3, CodeNotFound: 4,
		CodeConflict: 5, CodeTimeout: 8,
		CodeRateLimited: 9, CodeServerError: 10, CodeOperationFailed: 11, CodeInterrupted: 130,
	}
	for code, want := range pinned {
		if got := (&Error{Code: code}).ExitCode(); got != want {
			t.Errorf("%s exits %d, want %d", code, got, want)
		}
	}
}

// TestInterruptedFollowsTheShellConvention: 130 is what a shell reports for SIGINT, and a CLI
// that exits something else after Ctrl-C breaks the caller's own signal handling.
func TestInterruptedFollowsTheShellConvention(t *testing.T) {
	if got := (&Error{Code: CodeInterrupted}).ExitCode(); got != 130 {
		t.Fatalf("interrupted exits %d, want 130", got)
	}
}

func TestAnUnknownCodeExitsInternal(t *testing.T) {
	if got := (&Error{Code: Code("MADE_UP")}).ExitCode(); got != 1 {
		t.Fatalf("an unknown code exits %d, want 1", got)
	}
}

func TestNewFormatsAndCarriesDocs(t *testing.T) {
	e := New(CodeNotFound, "no cluster named %q", "iad-1")
	if e.Error() != `no cluster named "iad-1"` {
		t.Errorf("message = %q", e.Error())
	}
	if e.Code != CodeNotFound {
		t.Errorf("code = %v", e.Code)
	}
	if e.Docs != DocsURL || !strings.HasPrefix(e.Docs, "https://github.com/hangar-dev/hangar/") {
		t.Errorf("docs = %q, want the error table in the repository", e.Docs)
	}
}

// TestSuccessCarriesNoDocsLink: there is no page for "it worked".
func TestSuccessCarriesNoDocsLink(t *testing.T) {
	if got := New(CodeOK, "fine").Docs; got != "" {
		t.Fatalf("OK carries a docs link: %q", got)
	}
}

// TestWrapKeepsTheOriginalReachable. errors.Is and errors.As have to keep working through the
// typed contract, or a caller loses the ability to test for a sentinel underneath.
func TestWrapKeepsTheOriginalReachable(t *testing.T) {
	sentinel := errors.New("connection refused")
	e := Wrap(sentinel, CodeNetwork, "cannot reach %s", "plane.example")

	if !errors.Is(e, sentinel) {
		t.Error("the wrapped error is no longer reachable with errors.Is")
	}
	if e.Cause != "connection refused" {
		t.Errorf("cause = %q, want the original message", e.Cause)
	}
	if e.Error() != "cannot reach plane.example" {
		t.Errorf("message = %q, want the contextual one", e.Error())
	}
}

func TestWrapTolteratesANilCause(t *testing.T) {
	e := Wrap(nil, CodeInternal, "something went wrong")
	if e.Cause != "" {
		t.Errorf("cause = %q, want empty", e.Cause)
	}
	if e.Unwrap() != nil {
		t.Error("wrapping nil produced something to unwrap")
	}
}

// TestAsSynthesisesForAnythingUntyped: every failure path has to produce one of these so that
// human, JSON and agent renderings stay identical in substance.
func TestAsSynthesisesForAnythingUntyped(t *testing.T) {
	if As(nil) != nil {
		t.Error("As(nil) invented an error")
	}

	plain := fmt.Errorf("something broke")
	got := As(plain)
	if got == nil {
		t.Fatal("As dropped a plain error")
	}
	if got.Code != CodeInternal {
		t.Errorf("code = %v, want internal", got.Code)
	}
	if got.Error() != "something broke" {
		t.Errorf("message = %q", got.Error())
	}
	if !errors.Is(got, plain) {
		t.Error("the plain error is no longer reachable")
	}
}

func TestAsReturnsATypedErrorUnchanged(t *testing.T) {
	original := New(CodeForbidden, "nope")
	if got := As(original); got != original {
		t.Fatal("As rebuilt an error that was already typed")
	}
	wrapped := fmt.Errorf("while listing: %w", original)
	if got := As(wrapped); got != original {
		t.Fatal("As did not find the typed error under a wrap")
	}
}

// TestABareMessageWithAPercentIsNotReformatted guards the one-argument call shape used by As.
func TestABareMessageWithAPercentIsNotReformatted(t *testing.T) {
	got := As(errors.New("100% full"))
	if got.Error() != "100% full" {
		t.Fatalf("message = %q, want it verbatim", got.Error())
	}
}

func TestBuildersCompose(t *testing.T) {
	e := New(CodeRateLimited, "too many requests").
		WithCause("the plane is shedding load").
		WithFix("Wait and try again", "hgr cluster ls").
		WithFix("Check your quota", "hgr quota ls").
		WithRetry(30).
		WithFlag("--force")

	if e.Cause != "the plane is shedding load" {
		t.Errorf("cause = %q", e.Cause)
	}
	if len(e.Remediation) != 2 {
		t.Fatalf("remediation has %d entries, want 2 in the order they were added", len(e.Remediation))
	}
	if e.Remediation[0].Command != "hgr cluster ls" || e.Remediation[1].Command != "hgr quota ls" {
		t.Errorf("remediation order = %+v", e.Remediation)
	}
	if !e.Retryable || e.RetryAfterS != 30 {
		t.Errorf("retry = %v after %d", e.Retryable, e.RetryAfterS)
	}
	if e.RequiredFlag != "--force" {
		t.Errorf("required flag = %q", e.RequiredFlag)
	}
}

func TestWithCausefFormats(t *testing.T) {
	e := New(CodeConflict, "over quota").WithCausef("%d of %d GPUs in use", 8, 8)
	if e.Cause != "8 of 8 GPUs in use" {
		t.Fatalf("cause = %q", e.Cause)
	}
}

// TestTheJSONShapeIsTheContract. Scripts and agents read this; a renamed field is a breaking
// change for every one of them, and the token-free fields must not appear when empty.
func TestTheJSONShapeIsTheContract(t *testing.T) {
	full, err := json.Marshal(New(CodeNotFound, "no such cluster").
		WithCause("looked in project prj_1").
		WithFix("List them", "hgr cluster ls").
		WithRetry(5).
		WithFlag("--project"))
	if err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{
		`"code":"NOT_FOUND"`, `"message":"no such cluster"`, `"cause":`,
		`"retryable":true`, `"retry_after_s":5`, `"remediation":`, `"docs":`, `"required_flag":"--project"`,
	} {
		if !strings.Contains(string(full), field) {
			t.Errorf("the JSON error is missing %s: %s", field, full)
		}
	}

	bare, err := json.Marshal(New(CodeUsage, "bad flag"))
	if err != nil {
		t.Fatal(err)
	}
	for _, absent := range []string{"cause", "retryable", "retry_after_s", "remediation", "required_flag"} {
		if strings.Contains(string(bare), `"`+absent+`"`) {
			t.Errorf("an unset %q was serialised anyway: %s", absent, bare)
		}
	}
}

// TestEveryExplainedCodeIsAKnownOne: `hgr explain` must not describe a code the CLI cannot emit.
func TestEveryExplainedCodeIsAKnownOne(t *testing.T) {
	known := map[Code]bool{}
	for _, c := range allCodes {
		known[c] = true
	}
	for _, c := range AllCodes() {
		if !known[c] {
			t.Errorf("`hgr explain` describes %s, which is not a code this package declares", c)
		}
	}
}

// TestEveryExplanationIsUsable: a summary and at least one fix, with the exit code filled in from
// the same table the process uses. An explanation with no next step is a dead end.
func TestEveryExplanationIsUsable(t *testing.T) {
	for _, c := range AllCodes() {
		e, ok := Explain(c)
		if !ok {
			t.Errorf("Explain(%s) came back empty for a code it lists", c)
			continue
		}
		if e.Code != c {
			t.Errorf("Explain(%s) identifies itself as %s", c, e.Code)
		}
		if e.Exit != exitCodes[c] {
			t.Errorf("Explain(%s) reports exit %d, the process uses %d", c, e.Exit, exitCodes[c])
		}
		if strings.TrimSpace(e.Summary) == "" {
			t.Errorf("Explain(%s) has no summary", c)
		}
		if len(e.Fixes) == 0 {
			t.Errorf("Explain(%s) offers no way forward", c)
		}
	}
}

func TestExplainRefusesACodeItDoesNotKnow(t *testing.T) {
	if _, ok := Explain(Code("MADE_UP")); ok {
		t.Fatal("Explain invented an explanation")
	}
}

func TestAllCodesIsSorted(t *testing.T) {
	got := AllCodes()
	if !sort.SliceIsSorted(got, func(i, j int) bool { return got[i] < got[j] }) {
		t.Fatalf("AllCodes is not sorted: %v", got)
	}
}

// TestParseCodeAcceptsHowAPersonWouldTypeIt: `hgr explain not-found` and `hgr explain NOT_FOUND`
// are the same request.
func TestParseCodeAcceptsHowAPersonWouldTypeIt(t *testing.T) {
	cases := map[string]Code{
		"not-found":     CodeNotFound,
		"NOT_FOUND":     CodeNotFound,
		"  not_found  ": CodeNotFound,
		"Rate-Limited":  CodeRateLimited,
		"invalid-state": CodeInvalidState,
	}
	for in, want := range cases {
		if got := ParseCode(in); got != want {
			t.Errorf("ParseCode(%q) = %v, want %v", in, got, want)
		}
	}
	if _, ok := Explain(ParseCode("not-found")); !ok {
		t.Error("a parsed code does not resolve to an explanation")
	}
}
