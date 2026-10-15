package output

import (
	"bytes"
	"encoding/json"
	"io"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
)

type cluster struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Region string `json:"region"`
	Ready  bool   `json:"ready"`
	Nodes  int    `json:"nodes"`
}

func sample() []cluster {
	return []cluster{
		{"clu_1", "iad-1", "us-east-1", true, 12},
		{"clu_2", "sin-1", "ap-southeast-1", false, 4},
	}
}

func render(t *testing.T, format Format, arg string, agent, quiet bool, res *Result) (string, string) {
	t.Helper()
	var out, errw bytes.Buffer
	r := New(&out, &errw, ui.Style{}, format, arg, 120, agent, quiet)
	if err := r.Emit(res); err != nil {
		t.Fatalf("emit %s: %v", format, err)
	}
	return out.String(), errw.String()
}

func TestParseFormatAcceptsEveryDeclaredMode(t *testing.T) {
	cases := map[string]Format{
		"auto": FormatAuto, "human": FormatHuman, "wide": FormatWide,
		"json": FormatJSON, "jsonl": FormatJSONL, "yaml": FormatYAML,
		"tsv": FormatTSV, "name": FormatName,
		"  json  ": FormatJSON,
		// `table` is what people type coming from kubectl.
		"table": FormatHuman,
	}
	for in, want := range cases {
		got, arg, err := ParseFormat(in)
		if err != nil {
			t.Errorf("ParseFormat(%q): %v", in, err)
			continue
		}
		if got != want {
			t.Errorf("ParseFormat(%q) = %v, want %v", in, got, want)
		}
		if arg != "" {
			t.Errorf("ParseFormat(%q) produced an argument %q", in, arg)
		}
	}
}

func TestParseFormatSplitsTheInlineArgument(t *testing.T) {
	f, arg, err := ParseFormat("jsonpath=.items[*].name")
	if err != nil {
		t.Fatal(err)
	}
	if f != FormatJSONPath || arg != ".items[*].name" {
		t.Fatalf("= %v %q", f, arg)
	}

	// A template can itself contain `=`; only the first separator counts.
	f, arg, err = ParseFormat(`template={{range .items}}{{.name}}={{.region}}{{end}}`)
	if err != nil {
		t.Fatal(err)
	}
	if f != FormatTemplate || !strings.Contains(arg, "{{.region}}") {
		t.Fatalf("= %v %q", f, arg)
	}
}

func TestParseFormatRefusesTheRestAndListsTheOptions(t *testing.T) {
	for _, bad := range []string{"", "xml", "JSON", "jsonpath", "nope=.x"} {
		_, _, err := ParseFormat(bad)
		if err == nil {
			t.Errorf("ParseFormat(%q) was accepted", bad)
			continue
		}
		e := errs.As(err)
		if e.Code != errs.CodeUsage {
			t.Errorf("ParseFormat(%q) is not a usage error", bad)
		}
		if !strings.Contains(e.Cause, "jsonpath") {
			t.Errorf("ParseFormat(%q) does not list the valid formats: %q", bad, e.Cause)
		}
	}
}

// TestMachineSaysWhichFormatsAProgramReads. It gates whether errors go to stderr as JSON, so a
// human format misclassified here would print a table into a pipe.
func TestMachineSaysWhichFormatsAProgramReads(t *testing.T) {
	for _, f := range []Format{FormatHuman, FormatWide, FormatAuto} {
		if f.Machine() {
			t.Errorf("%s is a human format", f)
		}
	}
	for _, f := range []Format{FormatJSON, FormatJSONL, FormatYAML, FormatTSV, FormatName, FormatTemplate, FormatJSONPath} {
		if !f.Machine() {
			t.Errorf("%s is read by a program", f)
		}
	}
}

// TestAgentModeAlwaysWrapsInTheEnvelope, whatever the format flag says. The envelope is the whole
// contract for an agent, and a bare listing would break every consumer of it.
func TestAgentModeAlwaysWrapsInTheEnvelope(t *testing.T) {
	for _, f := range []Format{FormatHuman, FormatJSON, FormatYAML, FormatTSV, FormatName} {
		out, _ := render(t, f, "", true, false, &Result{
			Command: "cluster ls", Data: sample(), Names: []string{"iad-1"},
		})
		var env Envelope
		if err := json.Unmarshal([]byte(out), &env); err != nil {
			t.Errorf("agent mode with --output %s did not produce an envelope: %v", f, err)
			continue
		}
		if env.SchemaVersion != SchemaVersion || !env.OK || env.Command != "cluster ls" {
			t.Errorf("envelope = %+v", env)
		}
	}
}

// TestTheEnvelopeNeverHasNullCollections — an agent iterating `warnings` must not have to check
// for null first, so absent means empty, not nil.
func TestTheEnvelopeNeverHasNullCollections(t *testing.T) {
	out, _ := render(t, FormatJSON, "", true, false, &Result{Command: "cluster ls"})
	for _, field := range []string{`"warnings":[]`, `"next_actions":[]`} {
		if !strings.Contains(strings.ReplaceAll(out, " ", ""), strings.ReplaceAll(field, " ", "")) {
			t.Errorf("the envelope is missing %s: %s", field, out)
		}
	}
	if strings.Contains(out, "null") {
		t.Errorf("the envelope contains a null: %s", out)
	}
}

func TestTheEnvelopeCarriesMetaAndDuration(t *testing.T) {
	var out, errw bytes.Buffer
	r := New(&out, &errw, ui.Style{}, FormatJSON, "", 120, true, false)
	r.AddMeta("context", "prod")
	if err := r.Emit(&Result{Command: "cluster ls", Meta: map[string]any{"page": 1}}); err != nil {
		t.Fatal(err)
	}
	var env Envelope
	if err := json.Unmarshal(out.Bytes(), &env); err != nil {
		t.Fatal(err)
	}
	if env.Meta["context"] != "prod" {
		t.Errorf("renderer-level meta was dropped: %v", env.Meta)
	}
	if _, ok := env.Meta["duration_ms"]; !ok {
		t.Error("the envelope does not report how long the command took")
	}
	if env.Meta["page"] != float64(1) {
		t.Errorf("result-level meta was dropped: %v", env.Meta)
	}
}

// TestAgentErrorsAreTheSameEnvelopeWithOKFalse, so one parser reads both outcomes.
func TestAgentErrorsAreTheSameEnvelope(t *testing.T) {
	var out, errw bytes.Buffer
	r := New(&out, &errw, ui.Style{}, FormatHuman, "", 120, true, false)
	code := r.EmitError("cluster get", errs.New(errs.CodeNotFound, "no cluster named iad-9").
		WithFix("List them", "hgr cluster ls"))

	if code != 4 {
		t.Errorf("exit = %d, want the code's own exit status", code)
	}
	var env Envelope
	if err := json.Unmarshal(out.Bytes(), &env); err != nil {
		t.Fatalf("agent error is not an envelope: %v (%s)", err, out.String())
	}
	if env.OK {
		t.Error("a failure reported ok:true")
	}
	if env.Error == nil || env.Error.Code != errs.CodeNotFound {
		t.Errorf("error = %+v", env.Error)
	}
	if len(env.NextActions) != 1 {
		t.Errorf("the remediation did not reach the envelope: %+v", env.NextActions)
	}
}

// TestAMachineFormatSendsErrorsToStderrAsJSON — stdout stays parseable even when the command
// failed, which is what lets `hgr ... -o json | jq` behave.
func TestAMachineFormatSendsErrorsToStderrAsJSON(t *testing.T) {
	var out, errw bytes.Buffer
	r := New(&out, &errw, ui.Style{}, FormatJSON, "", 120, false, false)
	code := r.EmitError("cluster get", errs.New(errs.CodeForbidden, "nope"))

	if code != 3 {
		t.Errorf("exit = %d", code)
	}
	if out.Len() != 0 {
		t.Errorf("stdout was polluted on failure: %q", out.String())
	}
	var body map[string]any
	if err := json.Unmarshal(errw.Bytes(), &body); err != nil {
		t.Fatalf("stderr is not JSON: %v (%s)", err, errw.String())
	}
	if body["error"] == nil {
		t.Error("the error body has no error in it")
	}
}

// TestAHumanErrorNamesTheCodeAndHowToExplainIt.
func TestAHumanErrorNamesTheCodeAndHowToExplainIt(t *testing.T) {
	var out, errw bytes.Buffer
	r := New(&out, &errw, ui.Style{}, FormatHuman, "", 120, false, false)
	r.EmitError("cluster get", errs.New(errs.CodeRateLimited, "no room").
		WithCause("every node is full").
		WithFix("Try another region", "hgr cluster ls --region ap-southeast-1").
		WithRetry(60))

	got := errw.String()
	for _, want := range []string{
		"no room", "every node is full", "hgr cluster ls --region ap-southeast-1",
		"RATE_LIMITED", "hgr explain RATE_LIMITED", "retryable in ~60s",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("the human error is missing %q:\n%s", want, got)
		}
	}
	if out.Len() != 0 {
		t.Errorf("the error was written to stdout: %q", out.String())
	}
}

func TestEmitErrorOnNilIsASuccess(t *testing.T) {
	var out, errw bytes.Buffer
	r := New(&out, &errw, ui.Style{}, FormatHuman, "", 120, false, false)
	if code := r.EmitError("cluster ls", nil); code != 0 {
		t.Fatalf("exit = %d for a nil error, want 0", code)
	}
	if out.Len() != 0 || errw.Len() != 0 {
		t.Error("a nil error printed something")
	}
}

func TestJSONIsIndentedAndJSONLIsNot(t *testing.T) {
	indented, _ := render(t, FormatJSON, "", false, false, &Result{Data: sample()})
	if !strings.Contains(indented, "\n  ") {
		t.Errorf("json is not indented: %s", indented)
	}

	lines, _ := render(t, FormatJSONL, "", false, false, &Result{Data: sample()})
	got := strings.Split(strings.TrimSpace(lines), "\n")
	if len(got) != 2 {
		t.Fatalf("jsonl produced %d lines for 2 items:\n%s", len(got), lines)
	}
	for i, line := range got {
		var one cluster
		if err := json.Unmarshal([]byte(line), &one); err != nil {
			t.Errorf("jsonl line %d is not one object: %v", i, err)
		}
	}
}

// TestJSONLOfANonSliceIsStillOneLine — a `get` returns a single object, and jsonl must not refuse
// it or wrap it in a list.
func TestJSONLOfANonSliceIsOneLine(t *testing.T) {
	out, _ := render(t, FormatJSONL, "", false, false, &Result{Data: sample()[0]})
	if n := len(strings.Split(strings.TrimSpace(out), "\n")); n != 1 {
		t.Fatalf("jsonl of one object gave %d lines:\n%s", n, out)
	}
}

// TestJSONOfNothingPrintsNothing — `hgr ... -o json` on a command with no data must not emit
// `null`, which every consumer would have to special-case.
func TestJSONOfNothingPrintsNothing(t *testing.T) {
	for _, f := range []Format{FormatJSON, FormatYAML} {
		out, _ := render(t, f, "", false, false, &Result{Command: "config set"})
		if strings.TrimSpace(out) != "" {
			t.Errorf("%s printed %q for a command with no data", f, out)
		}
	}
}

// TestYAMLGoesThroughJSONSoTagsDriveBoth — a field named one thing in JSON and another in YAML
// would make the two formats disagree about the same object.
func TestYAMLUsesTheJSONFieldNames(t *testing.T) {
	out, _ := render(t, FormatYAML, "", false, false, &Result{Data: sample()[0]})
	for _, want := range []string{"id: clu_1", "name: iad-1", "region: us-east-1", "ready: true", "nodes: 12"} {
		if !strings.Contains(out, want) {
			t.Errorf("yaml is missing %q:\n%s", want, out)
		}
	}
}

func TestTSVWritesTheProjectionItWasGiven(t *testing.T) {
	out, _ := render(t, FormatTSV, "", false, false, &Result{
		Columns: []string{"NAME", "REGION"},
		Rows:    [][]string{{"iad-1", "us-east-1"}, {"sin-1", "ap-southeast-1"}},
	})
	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) != 3 {
		t.Fatalf("tsv produced %d lines, want a header and two rows:\n%s", len(lines), out)
	}
	if lines[0] != "NAME\tREGION" {
		t.Errorf("header = %q", lines[0])
	}
	if lines[1] != "iad-1\tus-east-1" {
		t.Errorf("row = %q", lines[1])
	}
}

// TestTSVWithoutAProjectionFallsBackToJSON rather than printing nothing.
func TestTSVWithoutAProjectionFallsBackToJSON(t *testing.T) {
	out, _ := render(t, FormatTSV, "", false, false, &Result{Data: sample()})
	if !strings.Contains(out, `"id"`) {
		t.Fatalf("tsv with no rows printed %q, want the JSON fallback", out)
	}
}

func TestNameFormatPrintsOnlyNames(t *testing.T) {
	out, _ := render(t, FormatName, "", false, false, &Result{
		Data:  sample(),
		Names: []string{"iad-1", "sin-1"},
	})
	if out != "iad-1\nsin-1\n" {
		t.Fatalf("name output = %q", out)
	}
}

// TestQuietPrintsNamesOnStdoutAndNothingElse — `--quiet` exists so a name can be piped into the
// next command.
func TestQuietPrintsOnlyNames(t *testing.T) {
	out, errw := render(t, FormatHuman, "", false, true, &Result{
		Names:    []string{"iad-1"},
		Warnings: []string{"one cluster is unreachable"},
		Human:    func(w io.Writer, _ ui.Style, _ int) { _, _ = w.Write([]byte("a table")) },
		Next:     []errs.Action{{Description: "Look closer", Command: "hgr cluster get iad-1"}},
	})
	if out != "iad-1\n" {
		t.Errorf("quiet stdout = %q, want only the name", out)
	}
	if errw != "" {
		t.Errorf("quiet still wrote to stderr: %q", errw)
	}
}

// TestWarningsAndNextActionsGoToStderr so that stdout stays the data channel even in human mode.
func TestWarningsAndNextActionsGoToStderr(t *testing.T) {
	out, errw := render(t, FormatHuman, "", false, false, &Result{
		Warnings: []string{"one cluster is unreachable"},
		Human: func(w io.Writer, _ ui.Style, _ int) {
			_, _ = w.Write([]byte("a table\n"))
		},
		Next: []errs.Action{{Description: "Look closer", Command: "hgr cluster get iad-1"}},
	})
	if !strings.Contains(out, "a table") {
		t.Errorf("the table did not reach stdout: %q", out)
	}
	if !strings.Contains(errw, "one cluster is unreachable") {
		t.Errorf("the warning did not reach stderr: %q", errw)
	}
	if !strings.Contains(errw, "hgr cluster get iad-1") {
		t.Errorf("the next action did not reach stderr: %q", errw)
	}
	if strings.Contains(out, "unreachable") {
		t.Error("the warning was written into the data channel")
	}
}

// TestAWarningTheTableAlreadyDrewIsNotPrintedTwice.
func TestAWarningTheTableAlreadyDrewIsNotPrintedTwice(t *testing.T) {
	_, errw := render(t, FormatHuman, "", false, false, &Result{
		Warnings:      []string{"one cluster is unreachable"},
		WarningsShown: true,
	})
	if strings.Contains(errw, "unreachable") {
		t.Fatalf("the warning was printed a second time: %q", errw)
	}
}

// TestANextActionWithNoCommandIsNotOffered — there is nothing for the reader to run.
func TestANextActionWithNoCommandIsNotOffered(t *testing.T) {
	_, errw := render(t, FormatHuman, "", false, false, &Result{
		Next: []errs.Action{{Description: "Ask an operator"}},
	})
	if strings.Contains(errw, "Ask an operator") {
		t.Fatalf("an unrunnable action was offered: %q", errw)
	}
}

// TestASilentResultDrawsNothing — used by commands that stream their own output.
func TestASilentResultDrawsNothing(t *testing.T) {
	out, _ := render(t, FormatHuman, "", false, false, &Result{
		Silent: true,
		Human:  func(w io.Writer, _ ui.Style, _ int) { _, _ = w.Write([]byte("a table")) },
	})
	if out != "" {
		t.Fatalf("a silent result drew %q", out)
	}
}

func TestEmitToleratesANilResult(t *testing.T) {
	var out, errw bytes.Buffer
	r := New(&out, &errw, ui.Style{}, FormatHuman, "", 120, false, false)
	if err := r.Emit(nil); err != nil {
		t.Fatalf("emit nil: %v", err)
	}
}

func TestTemplateRendersAgainstTheGenericShape(t *testing.T) {
	out, _ := render(t, FormatTemplate, `{{range .}}{{.name | upper}} {{end}}`, false, false,
		&Result{Data: sample()})
	if strings.TrimSpace(out) != "IAD-1 SIN-1" {
		t.Fatalf("template output = %q", out)
	}
}

func TestAnInvalidTemplateIsAUsageError(t *testing.T) {
	var out, errw bytes.Buffer
	r := New(&out, &errw, ui.Style{}, FormatTemplate, `{{range .}}`, 120, false, false)
	err := r.Emit(&Result{Data: sample()})
	if err == nil {
		t.Fatal("an unterminated template was accepted")
	}
	if errs.As(err).Code != errs.CodeUsage {
		t.Errorf("code = %v, want usage", errs.As(err).Code)
	}
}

func TestJSONPathWalksTheSubsetItDocuments(t *testing.T) {
	data := map[string]any{
		"items": sample(),
		"page":  map[string]any{"count": 2, "next": nil},
	}
	cases := map[string]string{
		".items[0].name":  "iad-1",
		".items[1].name":  "sin-1",
		".items[*].name":  "iad-1\nsin-1",
		".page.count":     "2",
		"$.items[0].id":   "clu_1",
		".items[0].ready": "true",
		".items[0].nodes": "12",
		".page.next":      "",
	}
	for path, want := range cases {
		out, _ := render(t, FormatJSONPath, path, false, false, &Result{Data: data})
		if strings.TrimSpace(out) != strings.TrimSpace(want) {
			t.Errorf("jsonpath %q = %q, want %q", path, strings.TrimSpace(out), want)
		}
	}
}

// TestJSONPathProjectsAcrossAListOfObjects, which is the one thing `[*]` exists for.
func TestJSONPathProjectsAcrossAList(t *testing.T) {
	out, _ := render(t, FormatJSONPath, ".items[*].region", false, false,
		&Result{Data: map[string]any{"items": sample()}})
	if strings.TrimSpace(out) != "us-east-1\nap-southeast-1" {
		t.Fatalf("projection = %q", out)
	}
}

func TestJSONPathRefusesWhatItCannotWalk(t *testing.T) {
	data := map[string]any{"items": sample()}
	cases := map[string]errs.Code{
		".nope":             errs.CodeNotFound,
		".items[9].name":    errs.CodeNotFound,
		".items[0].nope":    errs.CodeNotFound,
		".items.name":       errs.CodeUsage,
		".items[0].name[0]": errs.CodeUsage,
	}
	for path, want := range cases {
		var out, errw bytes.Buffer
		r := New(&out, &errw, ui.Style{}, FormatJSONPath, path, 120, false, false)
		err := r.Emit(&Result{Data: data})
		if err == nil {
			t.Errorf("jsonpath %q was accepted", path)
			continue
		}
		if got := errs.As(err).Code; got != want {
			t.Errorf("jsonpath %q gave %v, want %v", path, got, want)
		}
	}
}

// TestStreamJSONIsAlwaysNewlineDelimited. Indenting it would break the one-object-per-line
// contract that follow-mode consumers read incrementally.
func TestStreamJSONIsAlwaysNewlineDelimited(t *testing.T) {
	for _, f := range []Format{FormatJSON, FormatJSONL, FormatHuman} {
		var out, errw bytes.Buffer
		r := New(&out, &errw, ui.Style{}, f, "", 120, false, false)
		r.StreamJSON(sample()[0])
		r.StreamJSON(sample()[1])

		lines := strings.Split(strings.TrimSpace(out.String()), "\n")
		if len(lines) != 2 {
			t.Errorf("StreamJSON under %s produced %d lines:\n%s", f, len(lines), out.String())
			continue
		}
		for _, line := range lines {
			var one cluster
			if err := json.Unmarshal([]byte(line), &one); err != nil {
				t.Errorf("StreamJSON under %s emitted a partial line: %v", f, err)
			}
		}
	}
}

// TestJSONPathProjectsThroughNestedFields — the wildcard has to carry the whole remaining path,
// not just one segment of it.
func TestJSONPathProjectsThroughNestedFields(t *testing.T) {
	data := map[string]any{"items": []any{
		map[string]any{"name": "iad-1", "status": map[string]any{"phase": "ready"}},
		map[string]any{"name": "sin-1", "status": map[string]any{"phase": "draining"}},
	}}
	out, _ := render(t, FormatJSONPath, ".items[*].status.phase", false, false, &Result{Data: data})
	if strings.TrimSpace(out) != "ready\ndraining" {
		t.Fatalf("nested projection = %q, want the two phases", strings.TrimSpace(out))
	}
}

// TestABareWildcardStillReturnsTheList — `.items[*]` with nothing after it means the items.
func TestABareWildcardReturnsTheList(t *testing.T) {
	out, _ := render(t, FormatJSONPath, ".items[*]", false, false,
		&Result{Data: map[string]any{"items": sample()}})
	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) != 2 {
		t.Fatalf("bare wildcard gave %d lines:\n%s", len(lines), out)
	}
	if !strings.Contains(lines[0], `"name":"iad-1"`) {
		t.Errorf("the wildcard did not return whole objects: %q", lines[0])
	}
}

// TestAProjectionThatMissesAFieldFailsRatherThanPrintingBlanks — a column that is silently empty
// for half the rows is worse than a refusal, because it reads as data.
func TestAProjectionThatMissesAFieldFails(t *testing.T) {
	data := map[string]any{"items": []any{
		map[string]any{"name": "iad-1"},
		map[string]any{"other": "sin-1"},
	}}
	var out, errw bytes.Buffer
	r := New(&out, &errw, ui.Style{}, FormatJSONPath, ".items[*].name", 120, false, false)
	if err := r.Emit(&Result{Data: data}); err == nil {
		t.Fatal("a projection over a field only some items have was accepted")
	}
}
