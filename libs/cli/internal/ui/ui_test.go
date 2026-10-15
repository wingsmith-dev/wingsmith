package ui

import (
	"bytes"
	"os"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
)

func plain() Style { return Style{Level: ColorNone, Unicode: true} }

func prompter(input string, enabled bool) (*Prompter, *bytes.Buffer) {
	out := &bytes.Buffer{}
	return NewPrompter(strings.NewReader(input), out, plain(), enabled), out
}

// --- prompting -------------------------------------------------------------

// TestPromptingWhenDisabledFailsRatherThanBlocking is the whole reason these methods return an
// error: an agent or a CI job with no terminal must not hang on a read that will never complete.
func TestPromptingWhenDisabledFailsRatherThanBlocking(t *testing.T) {
	p, _ := prompter("", false)

	_, err := p.Ask("Which cluster?", "", "--cluster")
	if err == nil {
		t.Fatal("Ask blocked-mode returned no error")
	}
	e := errs.As(err)
	if e.Code != errs.CodeInputRequired {
		t.Errorf("code = %v, want input required", e.Code)
	}
	if e.RequiredFlag != "--cluster" {
		t.Errorf("the error does not name the flag that would have answered it: %q", e.RequiredFlag)
	}
	if len(e.Remediation) == 0 {
		t.Error("the refusal offers no way to supply the value")
	}
}

// TestADefaultAnswersEvenWithPromptingDisabled — a value that has a sensible default is not a
// value the caller has to be asked for.
func TestADefaultAnswersEvenWithPromptingDisabled(t *testing.T) {
	p, out := prompter("", false)
	got, err := p.Ask("Which region?", "us-east-1", "--region")
	if err != nil {
		t.Fatalf("Ask: %v", err)
	}
	if got != "us-east-1" {
		t.Errorf("Ask = %q, want the default", got)
	}
	if out.Len() != 0 {
		t.Errorf("a disabled prompter still wrote to the terminal: %q", out.String())
	}
}

func TestAskReadsALineAndFallsBackToTheDefault(t *testing.T) {
	p, out := prompter("iad-1\n", true)
	got, err := p.Ask("Which cluster?", "sin-1", "--cluster")
	if err != nil {
		t.Fatal(err)
	}
	if got != "iad-1" {
		t.Errorf("Ask = %q, want what was typed", got)
	}
	if !strings.Contains(out.String(), "Which cluster?") {
		t.Errorf("the question was not asked: %q", out.String())
	}
	if !strings.Contains(out.String(), "sin-1") {
		t.Errorf("the default was not shown: %q", out.String())
	}

	p, _ = prompter("\n", true)
	if got, _ := p.Ask("Which cluster?", "sin-1", ""); got != "sin-1" {
		t.Errorf("an empty answer gave %q, want the default", got)
	}
}

// TestAskOnAClosedStreamIsAnErrorNotAnEmptyAnswer — an empty string accepted as an answer would
// become a request against the wrong object.
func TestAskOnAClosedStreamIsAnError(t *testing.T) {
	p, _ := prompter("", true)
	if _, err := p.Ask("Which cluster?", "", "--cluster"); err == nil {
		t.Fatal("a closed stream was read as an empty answer")
	}
}

// TestAskAcceptsALastLineWithNoNewline — a heredoc or a piped answer often has none.
func TestAskAcceptsALastLineWithNoNewline(t *testing.T) {
	p, _ := prompter("iad-1", true)
	got, err := p.Ask("Which cluster?", "", "")
	if err != nil {
		t.Fatalf("Ask: %v", err)
	}
	if got != "iad-1" {
		t.Errorf("Ask = %q, want the unterminated line", got)
	}
}

// TestConfirmDefaultsToNo. The prompt reads [y/N] and a destructive operation must take silence
// as a refusal.
func TestConfirmDefaultsToNo(t *testing.T) {
	for _, answer := range []string{"\n", "n\n", "no\n", "maybe\n", "Y E S\n"} {
		p, _ := prompter(answer, true)
		if err := p.Confirm("Drain iad-1-n01?", false); err == nil {
			t.Errorf("Confirm(%q) was taken as consent", answer)
		}
	}
	for _, answer := range []string{"y\n", "Y\n", "yes\n", "YES\n", " yes \n"} {
		p, _ := prompter(answer, true)
		if err := p.Confirm("Drain iad-1-n01?", false); err != nil {
			t.Errorf("Confirm(%q) was refused: %v", answer, err)
		}
	}
}

func TestAssumeYesSkipsTheQuestionEntirely(t *testing.T) {
	p, out := prompter("", false)
	if err := p.Confirm("Drain iad-1-n01?", true); err != nil {
		t.Fatalf("--yes was not honoured: %v", err)
	}
	if out.Len() != 0 {
		t.Errorf("--yes still printed a question: %q", out.String())
	}
}

// TestConfirmWithNoTerminalNamesTheFlagThatWouldHaveAnsweredIt.
func TestConfirmWithNoTerminalNamesTheFlag(t *testing.T) {
	p, _ := prompter("", false)
	err := p.Confirm("Drain iad-1-n01?", false)
	if err == nil {
		t.Fatal("a destructive operation proceeded with no confirmation")
	}
	e := errs.As(err)
	if e.Code != errs.CodeConfirmationRequired {
		t.Errorf("code = %v, want confirmation required", e.Code)
	}
	if e.RequiredFlag != "--yes" {
		t.Errorf("required flag = %q, want --yes", e.RequiredFlag)
	}
}

func TestSelectByIndexAndByName(t *testing.T) {
	choices := []Choice{
		{Value: "clu_1", Label: "iad-1", Note: "us-east-1"},
		{Value: "clu_2", Label: "sin-1", Note: "ap-southeast-1"},
	}
	cases := map[string]string{
		"1\n":     "clu_1",
		"2\n":     "clu_2",
		"\n":      "clu_1", // the offered default
		"clu_2\n": "clu_2",
		"CLU_2\n": "clu_2",
	}
	for input, want := range cases {
		p, _ := prompter(input, true)
		got, err := p.Select("Which cluster?", choices, "--cluster")
		if err != nil {
			t.Errorf("Select(%q): %v", input, err)
			continue
		}
		if got != want {
			t.Errorf("Select(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestSelectRefusesSomethingNotOffered(t *testing.T) {
	choices := []Choice{{Value: "clu_1", Label: "iad-1"}}
	for _, input := range []string{"9\n", "0\n", "-1\n", "nonsense\n"} {
		p, _ := prompter(input, true)
		if _, err := p.Select("Which cluster?", choices, ""); err == nil {
			t.Errorf("Select accepted %q", input)
		}
	}
}

// TestSelectWithNothingToOfferSaysSo rather than presenting an empty list.
func TestSelectWithNothingToOfferSaysSo(t *testing.T) {
	p, _ := prompter("1\n", true)
	_, err := p.Select("Which cluster?", nil, "--cluster")
	if err == nil {
		t.Fatal("an empty picker was rendered")
	}
	if errs.As(err).Code != errs.CodeNotFound {
		t.Errorf("code = %v, want not found", errs.As(err).Code)
	}
}

func TestSelectDrawsEveryOptionWithItsNote(t *testing.T) {
	choices := []Choice{
		{Value: "clu_1", Label: "iad-1", Note: "us-east-1"},
		{Value: "clu_2", Label: "a-much-longer-name", Note: "ap-southeast-1"},
	}
	p, out := prompter("1\n", true)
	if _, err := p.Select("Which cluster?", choices, ""); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"iad-1", "a-much-longer-name", "us-east-1", "ap-southeast-1", "1)", "2)"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("the picker is missing %q:\n%s", want, out.String())
		}
	}
}

// --- style -----------------------------------------------------------------

// TestNoColourMeansNoEscapes — output redirected to a file or a pipe must be plain text.
func TestNoColourMeansNoEscapes(t *testing.T) {
	s := Style{Level: ColorNone}
	for name, got := range map[string]string{
		"Dim": s.Dim("x"), "Green": s.Green("x"), "Cyan": s.Cyan("x"), "Red": s.Red("x"),
		"Amber": s.Amber("x"), "Bold": s.Bold("x"), "Headline": s.Headline("x"),
		"Magenta": s.Magenta("x"), "ID": s.ID("x"), "Text": s.Text("x"), "Underline": s.Underline("x"),
	} {
		if strings.Contains(got, "\x1b") {
			t.Errorf("%s emitted an escape sequence at ColorNone: %q", name, got)
		}
	}
}

// TestEveryColourLevelKeepsTheText — whatever the escapes, the words have to survive, or a
// listing becomes unreadable on a terminal we guessed wrong about.
func TestEveryColourLevelKeepsTheText(t *testing.T) {
	for _, level := range []ColorLevel{ColorNone, Color16, Color256, ColorTrue} {
		s := Style{Level: level}
		got := s.Green("ready")
		if !strings.Contains(got, "ready") {
			t.Errorf("level %d lost the text: %q", level, got)
		}
		if level != ColorNone && !strings.Contains(got, "\x1b") {
			t.Errorf("level %d emitted no colour at all: %q", level, got)
		}
	}
}

func TestPaintingNothingIsNothing(t *testing.T) {
	s := Style{Level: ColorTrue}
	if got := s.Green(""); got != "" {
		t.Fatalf("painting an empty string gave %q", got)
	}
}

// TestSymbolsDegradeToASCII — a terminal that cannot render ✓ must not print a replacement box
// where a status should be.
func TestSymbolsDegradeToASCII(t *testing.T) {
	uni := Style{Level: ColorNone, Unicode: true}
	ascii := Style{Level: ColorNone, Unicode: false}

	pairs := []struct {
		name       string
		uni, ascii string
	}{
		{"Tick", uni.Tick(), ascii.Tick()},
		{"Cross", uni.Cross(), ascii.Cross()},
		{"Arrow", uni.Arrow(), ascii.Arrow()},
		{"Hook", uni.Hook(), ascii.Hook()},
		{"Dot", uni.Dot(), ascii.Dot()},
	}
	for _, p := range pairs {
		if p.uni == p.ascii {
			t.Errorf("%s is the same with and without unicode: %q", p.name, p.uni)
		}
		for _, r := range p.ascii {
			if r > 127 {
				t.Errorf("%s ASCII fallback still contains %q", p.name, r)
			}
		}
	}
}

// TestStatusColourIsCaseInsensitiveAndAlwaysReturnsTheStatus.
func TestStatusColourAlwaysReturnsTheStatus(t *testing.T) {
	s := Style{Level: ColorNone}
	for _, status := range []string{
		"running", "READY", "Pending", "failed", "stopped", "something-we-do-not-know", "",
	} {
		if got := s.StatusColor(status); got != status {
			t.Errorf("StatusColor(%q) = %q; the text must survive", status, got)
		}
	}
}

func TestStatusColourGroupsStatusesByMeaning(t *testing.T) {
	s := Style{Level: ColorTrue}
	same := func(a, b string) bool {
		return strings.TrimSuffix(strings.SplitAfter(s.StatusColor(a), "m")[0], "m") ==
			strings.TrimSuffix(strings.SplitAfter(s.StatusColor(b), "m")[0], "m")
	}
	if !same("running", "READY") {
		t.Error("running and ready are not drawn alike")
	}
	if !same("failed", "unhealthy") {
		t.Error("failed and unhealthy are not drawn alike")
	}
	if same("running", "failed") {
		t.Error("a healthy and a failed status are drawn the same colour")
	}
}

// TestLinksAreOnlyEmittedWhenTheTerminalHonoursThem — an OSC-8 sequence in a terminal that does
// not understand it prints as garbage around the label.
func TestLinksAreOnlyEmittedWhenSupported(t *testing.T) {
	off := Style{Level: ColorNone, Links: false}
	if got := off.Link("docs", "https://docs.hangar.dev"); strings.Contains(got, "\x1b]8") {
		t.Errorf("a link was emitted to a terminal that does not support it: %q", got)
	} else if !strings.Contains(got, "docs") {
		t.Errorf("the label was lost: %q", got)
	}

	on := Style{Level: ColorTrue, Links: true}
	got := on.Link("docs", "https://docs.hangar.dev")
	if !strings.Contains(got, "https://docs.hangar.dev") || !strings.Contains(got, "docs") {
		t.Errorf("the link is missing its target or label: %q", got)
	}
}

// --- widths and tables -----------------------------------------------------

// TestDisplayWidthIgnoresEscapes — a coloured cell is the same width on screen as a plain one,
// and counting the escapes would misalign every column after it.
func TestDisplayWidthIgnoresEscapes(t *testing.T) {
	s := Style{Level: ColorTrue}
	if plain, painted := DisplayWidth("ready"), DisplayWidth(s.Green("ready")); plain != painted {
		t.Fatalf("plain %d vs painted %d; escapes are being counted as width", plain, painted)
	}
}

func TestPadAndTruncateRespectDisplayWidth(t *testing.T) {
	if got := DisplayWidth(Pad("ab", 5)); got != 5 {
		t.Errorf("Pad width = %d, want 5", got)
	}
	if got := DisplayWidth(PadLeft("ab", 5)); got != 5 {
		t.Errorf("PadLeft width = %d, want 5", got)
	}
	if got := Pad("abcdef", 3); DisplayWidth(got) < 3 {
		t.Errorf("Pad shrank a cell below its own content: %q", got)
	}
	if got := DisplayWidth(Truncate("abcdefghij", 5)); got > 5 {
		t.Errorf("Truncate produced %d columns, want at most 5", got)
	}
}

func TestATableDrawsItsHeadersAndRows(t *testing.T) {
	var buf bytes.Buffer
	tb := NewTable(plain(), 120, false,
		Column{Title: "NAME"}, Column{Title: "REGION"}, Column{Title: "NODES", Align: AlignRight})
	tb.Row("iad-1", "us-east-1", "12")
	tb.Row("sin-1", "ap-southeast-1", "4")
	tb.Render(&buf)

	got := buf.String()
	for _, want := range []string{"NAME", "REGION", "NODES", "iad-1", "ap-southeast-1", "12"} {
		if !strings.Contains(got, want) {
			t.Errorf("the table is missing %q:\n%s", want, got)
		}
	}
	if n := len(strings.Split(strings.TrimSpace(got), "\n")); n < 3 {
		t.Errorf("the table drew %d lines, want a header and two rows:\n%s", n, got)
	}
}

func TestAnEmptyTableStillDrawsItsHeader(t *testing.T) {
	var buf bytes.Buffer
	tb := NewTable(plain(), 120, false, Column{Title: "NAME"})
	tb.Render(&buf)
	if !strings.Contains(buf.String(), "NAME") {
		t.Fatalf("an empty table drew nothing: %q", buf.String())
	}
}

// TestWideOnlyColumnsAppearOnlyInWideMode — that is what the flag is for.
func TestWideOnlyColumnsAppearOnlyInWideMode(t *testing.T) {
	draw := func(wide bool) string {
		var buf bytes.Buffer
		tb := NewTable(plain(), 200, wide, Column{Title: "NAME"}, Column{Title: "UID", Wide: true})
		tb.Row("iad-1", "clu_01j9x2")
		tb.Render(&buf)
		return buf.String()
	}
	if strings.Contains(draw(false), "UID") {
		t.Error("a wide-only column was drawn in the narrow table")
	}
	if !strings.Contains(draw(true), "UID") {
		t.Error("a wide-only column was missing from the wide table")
	}
}

func TestKVAlignsItsValues(t *testing.T) {
	var buf bytes.Buffer
	kv := NewKV(plain())
	kv.Add("cause", "every node is full")
	kv.Addf("retry", "in ~%ds", 60)
	kv.Add("", "a continuation line")
	kv.Add("code", "CAPACITY_UNAVAILABLE")
	kv.Render(&buf)

	got := buf.String()
	for _, want := range []string{"cause", "every node is full", "in ~60s", "a continuation line", "CAPACITY_UNAVAILABLE"} {
		if !strings.Contains(got, want) {
			t.Errorf("the KV block is missing %q:\n%s", want, got)
		}
	}
}

// TestAnEmptyValueIsNotDrawn — an error with no cause must not print a blank `cause` row.
func TestKVSkipsEmptyValues(t *testing.T) {
	var buf bytes.Buffer
	kv := NewKV(plain())
	kv.Add("cause", "")
	kv.Render(&buf)
	if strings.Contains(buf.String(), "cause") {
		t.Fatalf("an empty value was drawn: %q", buf.String())
	}
}

func TestBarIsBoundedAtBothEnds(t *testing.T) {
	s := plain()
	for _, f := range []float64{-1, 0, 0.5, 1, 2} {
		got := Bar(s, f, 10)
		if w := DisplayWidth(got); w != 10 {
			t.Errorf("Bar(%v) is %d columns, want exactly 10: %q", f, w, got)
		}
	}
}

func TestSparklineDrawsOneMarkPerValue(t *testing.T) {
	s := plain()
	if got := Sparkline(s, nil); got != "" {
		t.Errorf("a sparkline of nothing = %q", got)
	}
	got := Sparkline(s, []float64{0, 1, 2, 3, 4, 5})
	if DisplayWidth(got) != 6 {
		t.Errorf("Sparkline drew %d marks for 6 values: %q", DisplayWidth(got), got)
	}
	// A flat series must not divide by a zero range.
	flat := Sparkline(s, []float64{2, 2, 2})
	if DisplayWidth(flat) != 3 {
		t.Errorf("a flat series drew %q", flat)
	}
}

// /dev/null is a character device and not a terminal, which is the case a
// mode-bit check gets wrong and the width and colour decisions depend on.
func TestDevNullIsNotATerminal(t *testing.T) {
	f, err := os.Open(os.DevNull)
	if err != nil {
		t.Skip(err)
	}
	defer f.Close()
	if IsTerminal(f) {
		t.Fatal("/dev/null reported as a terminal")
	}
	t.Setenv("COLUMNS", "")
	if w := Width(f); w != 100 {
		t.Fatalf("width of a non-terminal = %d, want the 100-column default", w)
	}
}
