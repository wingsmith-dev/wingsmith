package ui

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"golang.org/x/term"
)

// Prompter collects input from a human. Every method fails with a typed error
// when input is disabled, so agent and CI paths never hang on a blocked read.
type Prompter struct {
	In      io.Reader
	Out     io.Writer
	Style   Style
	Enabled bool
}

// NewPrompter builds a prompter bound to the given streams.
func NewPrompter(in io.Reader, out io.Writer, style Style, enabled bool) *Prompter {
	return &Prompter{In: in, Out: out, Style: style, Enabled: enabled}
}

func (p *Prompter) disabled(what, flag string) error {
	e := errs.New(errs.CodeInputRequired, "%s is required and prompting is disabled.", what).
		WithCause("stdin is not an interactive terminal, or --no-input / agent mode is active")
	if flag != "" {
		e = e.WithFlag(flag).WithFix("Pass the value explicitly", flag+" <value>")
	}
	return e
}

func (p *Prompter) read() (string, error) {
	r := bufio.NewReader(p.In)
	line, err := r.ReadString('\n')
	if err != nil && line == "" {
		return "", errs.Wrap(err, errs.CodeInputRequired, "could not read input")
	}
	return strings.TrimSpace(line), nil
}

// Ask requests a free-text value, returning def when the user submits nothing.
func (p *Prompter) Ask(question, def, flag string) (string, error) {
	if !p.Enabled {
		if def != "" {
			return def, nil
		}
		return "", p.disabled(question, flag)
	}
	suffix := ""
	if def != "" {
		suffix = p.Style.Dim(" (" + def + ")")
	}
	fmt.Fprintf(p.Out, "%s %s%s ", p.Style.Cyan("?"), p.Style.Bold(question), suffix)
	v, err := p.read()
	if err != nil {
		return "", err
	}
	if v == "" {
		return def, nil
	}
	return v, nil
}

// Secret requests a value without echoing it, for a password or token. It
// turns the terminal's echo off around the read when stdin is a terminal, and
// falls back to a plain line read otherwise (a pipe carries no echo anyway).
func (p *Prompter) Secret(question, flag string) (string, error) {
	if !p.Enabled {
		return "", p.disabled(question, flag)
	}
	fmt.Fprintf(p.Out, "%s %s ", p.Style.Cyan("?"), p.Style.Bold(question))
	if f, ok := p.In.(*os.File); ok && term.IsTerminal(int(f.Fd())) {
		b, err := term.ReadPassword(int(f.Fd()))
		fmt.Fprintln(p.Out)
		if err != nil {
			return "", errs.Wrap(err, errs.CodeInputRequired, "could not read input")
		}
		return strings.TrimSpace(string(b)), nil
	}
	return p.read()
}

// Confirm asks a yes/no question. `assumeYes` short-circuits to true.
func (p *Prompter) Confirm(question string, assumeYes bool) error {
	if assumeYes {
		return nil
	}
	if !p.Enabled {
		return errs.New(errs.CodeConfirmationRequired, "%s", question).
			WithCause("this operation is destructive and no interactive terminal is available").
			WithFlag("--yes")
	}
	fmt.Fprintf(p.Out, "%s %s %s ", p.Style.Amber("?"), p.Style.Bold(question), p.Style.Dim("[y/N]"))
	v, err := p.read()
	if err != nil {
		return err
	}
	switch strings.ToLower(v) {
	case "y", "yes":
		return nil
	default:
		return errs.New(errs.CodeConfirmationRequired, "aborted by user")
	}
}

// Choice is a single selectable option.
type Choice struct {
	Value string
	Label string
	Note  string
}

// Select renders a numbered picker and returns the chosen value.
func (p *Prompter) Select(question string, choices []Choice, flag string) (string, error) {
	if len(choices) == 0 {
		return "", errs.New(errs.CodeNotFound, "no options available for %s", question)
	}
	if !p.Enabled {
		return "", p.disabled(question, flag)
	}
	fmt.Fprintf(p.Out, "%s %s\n", p.Style.Cyan("?"), p.Style.Bold(question))
	w := 0
	for _, c := range choices {
		if n := DisplayWidth(c.Label); n > w {
			w = n
		}
	}
	for i, c := range choices {
		fmt.Fprintf(p.Out, "  %s %s  %s\n",
			p.Style.Dim(PadLeft(strconv.Itoa(i+1), 2)+")"),
			Pad(c.Label, w), p.Style.Dim(c.Note))
	}
	fmt.Fprintf(p.Out, "%s ", p.Style.Dim("select [1]:"))
	v, err := p.read()
	if err != nil {
		return "", err
	}
	if v == "" {
		return choices[0].Value, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 1 || n > len(choices) {
		// Accept a value typed out in full as well as an index.
		for _, c := range choices {
			if strings.EqualFold(c.Value, v) {
				return c.Value, nil
			}
		}
		return "", errs.New(errs.CodeUsage, "%q is not one of the offered options", v)
	}
	return choices[n-1].Value, nil
}

// StdinIsTerminal reports whether input can be prompted for.
func StdinIsTerminal() bool { return IsTerminal(os.Stdin) }
