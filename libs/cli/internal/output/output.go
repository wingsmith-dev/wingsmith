// Package output turns a command's result into whichever representation the
// caller asked for. Commands never format for a specific consumer themselves.
package output

import (
	"encoding/json"
	"fmt"
	"io"
	"reflect"
	"strconv"
	"strings"
	"text/template"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
	yaml "go.yaml.in/yaml/v2"
)

// SchemaVersion is the agent-mode envelope contract version.
const SchemaVersion = "1"

// Format is a rendering mode.
type Format string

const (
	FormatAuto     Format = "auto"
	FormatHuman    Format = "human"
	FormatWide     Format = "wide"
	FormatJSON     Format = "json"
	FormatJSONL    Format = "jsonl"
	FormatYAML     Format = "yaml"
	FormatTSV      Format = "tsv"
	FormatName     Format = "name"
	FormatTemplate Format = "template"
	FormatJSONPath Format = "jsonpath"
)

// ParseFormat validates a --output value, extracting any inline argument.
func ParseFormat(s string) (Format, string, error) {
	s = strings.TrimSpace(s)
	if i := strings.IndexAny(s, "="); i > 0 {
		head, arg := s[:i], s[i+1:]
		switch Format(head) {
		case FormatTemplate:
			return FormatTemplate, arg, nil
		case FormatJSONPath:
			return FormatJSONPath, arg, nil
		}
		return "", "", badFormat(head)
	}
	switch Format(s) {
	case FormatAuto, FormatHuman, FormatWide, FormatJSON, FormatJSONL, FormatYAML, FormatTSV, FormatName:
		return Format(s), "", nil
	case "table":
		return FormatHuman, "", nil
	}
	return "", "", badFormat(s)
}

// badFormat carries no remediation of its own: only the caller knows the
// invocation, and a bare flag is not something anyone can run.
func badFormat(s string) error {
	return errs.New(errs.CodeUsage, "unknown output format %q", s).
		WithCause("valid formats: auto, human, wide, json, jsonl, yaml, tsv, name, template=…, jsonpath=…")
}

// Machine reports whether the format is meant for a program.
func (f Format) Machine() bool {
	switch f {
	case FormatHuman, FormatWide, FormatAuto:
		return false
	}
	return true
}

// Result is what every command returns. Data is the machine representation;
// Human draws the terminal representation. Both describe the same thing.
type Result struct {
	Command  string
	Data     any
	Human    func(w io.Writer, s ui.Style, width int)
	Names    []string
	Next     []errs.Action
	Warnings []string
	Meta     map[string]any
	// Rows supplies the TSV projection; without it TSV falls back to JSON.
	Columns []string
	Rows    [][]string
	// Silent suppresses all human output (used by commands that stream).
	Silent bool
	// WarningsShown marks that Human already renders Warnings, so the human
	// path must not print them a second time. Machine formats are unaffected.
	WarningsShown bool
}

// Envelope is the agent-mode wire format.
type Envelope struct {
	SchemaVersion string         `json:"schema_version"`
	OK            bool           `json:"ok"`
	Command       string         `json:"command"`
	Data          any            `json:"data,omitempty"`
	Error         *errs.Error    `json:"error,omitempty"`
	Warnings      []string       `json:"warnings"`
	NextActions   []errs.Action  `json:"next_actions"`
	Meta          map[string]any `json:"meta"`
}

// Renderer writes results to the caller's streams.
type Renderer struct {
	Out       io.Writer
	Err       io.Writer
	Style     ui.Style
	Format    Format
	Arg       string
	Width     int
	Agent     bool
	Quiet     bool
	Reporter  ui.Reporter
	Start     time.Time
	extraMeta map[string]any
}

// New builds a renderer.
func New(out, errw io.Writer, style ui.Style, format Format, arg string, width int, agent, quiet bool) *Renderer {
	return &Renderer{Out: out, Err: errw, Style: style, Format: format, Arg: arg,
		Width: width, Agent: agent, Quiet: quiet, Start: time.Now(), extraMeta: map[string]any{}}
}

// AddMeta records a value included in every agent-mode envelope.
func (r *Renderer) AddMeta(key string, value any) { r.extraMeta[key] = value }

func (r *Renderer) meta(extra map[string]any) map[string]any {
	m := map[string]any{"duration_ms": time.Since(r.Start).Milliseconds()}
	for k, v := range r.extraMeta {
		m[k] = v
	}
	for k, v := range extra {
		m[k] = v
	}
	return m
}

// Emit renders a successful result.
func (r *Renderer) Emit(res *Result) error {
	if res == nil {
		res = &Result{}
	}
	if r.Reporter != nil {
		r.Reporter.Stop()
	}
	if r.Agent {
		return r.writeJSON(Envelope{
			SchemaVersion: SchemaVersion,
			OK:            true,
			Command:       res.Command,
			Data:          res.Data,
			Warnings:      nonNil(res.Warnings),
			NextActions:   nonNilActions(res.Next),
			Meta:          r.meta(res.Meta),
		})
	}

	switch r.Format {
	case FormatJSON:
		if res.Data == nil {
			return nil
		}
		return r.writeJSON(res.Data)
	case FormatJSONL:
		return r.writeJSONL(res.Data)
	case FormatYAML:
		if res.Data == nil {
			return nil
		}
		return r.writeYAML(res.Data)
	case FormatTSV:
		return r.writeTSV(res)
	case FormatName:
		for _, n := range res.Names {
			fmt.Fprintln(r.Out, n)
		}
		return nil
	case FormatTemplate:
		return r.writeTemplate(res.Data)
	case FormatJSONPath:
		return r.writeJSONPath(res.Data)
	}

	// Human path.
	if r.Quiet {
		for _, n := range res.Names {
			fmt.Fprintln(r.Out, n)
		}
		return nil
	}
	if !res.WarningsShown {
		for _, w := range res.Warnings {
			fmt.Fprintf(r.Err, "%s %s\n", r.Style.Bang(), r.Style.Amber(w))
		}
	}
	if res.Human != nil && !res.Silent {
		res.Human(r.Out, r.Style, r.Width)
	}
	for _, a := range res.Next {
		if a.Command == "" {
			continue
		}
		fmt.Fprintf(r.Err, "%s %s  %s\n", r.Style.Hook(), r.Style.Cyan(a.Command), r.Style.Dim(a.Description))
	}
	return nil
}

// EmitError renders a failure and returns the process exit code.
func (r *Renderer) EmitError(command string, err error) int {
	e := errs.As(err)
	if e == nil {
		return 0
	}
	if r.Reporter != nil {
		r.Reporter.Stop()
	}
	if r.Agent {
		_ = r.writeJSON(Envelope{
			SchemaVersion: SchemaVersion,
			OK:            false,
			Command:       command,
			Error:         e,
			Warnings:      []string{},
			NextActions:   nonNilActions(e.Remediation),
			Meta:          r.meta(nil),
		})
		return e.ExitCode()
	}
	if r.Format.Machine() {
		_ = r.writeJSONTo(r.Err, map[string]any{"error": e})
		return e.ExitCode()
	}

	s := r.Style
	fmt.Fprintf(r.Err, "%s %s\n", s.Cross(), s.Text(e.Message))
	// Every row goes through one KV so the value column stays aligned no
	// matter which rows are present.
	kv := ui.NewKV(s)
	kv.Add("cause", e.Cause)
	for i, a := range e.Remediation {
		key := ""
		if i == 0 {
			key = "try"
		}
		kv.Add(key, s.Cyan(a.Command)+dimNote(s, a.Description))
	}
	if e.Retryable && e.RetryAfterS > 0 {
		kv.Addf("retry", "retryable in ~%ds", e.RetryAfterS)
	}
	kv.Add("code", s.Amber(string(e.Code))+" "+s.Dim(s.Dot())+" "+s.Dim("hgr explain "+string(e.Code)))
	kv.Render(r.Err)
	return e.ExitCode()
}

func dimNote(s ui.Style, note string) string {
	if note == "" {
		return ""
	}
	return "  " + s.Dim(note)
}

// StreamJSON writes one JSON object per line; used by follow-mode commands.
// A stream is always newline-delimited: indenting it would break the one
// object per line contract that consumers read incrementally.
func (r *Renderer) StreamJSON(v any) {
	_ = json.NewEncoder(r.Out).Encode(v)
}

func (r *Renderer) writeJSON(v any) error { return r.writeJSONTo(r.Out, v) }

func (r *Renderer) writeJSONTo(w io.Writer, v any) error {
	enc := json.NewEncoder(w)
	if r.Format != FormatJSONL {
		enc.SetIndent("", "  ")
	}
	return enc.Encode(v)
}

func (r *Renderer) writeJSONL(v any) error {
	rv := reflect.ValueOf(v)
	if rv.Kind() == reflect.Slice {
		enc := json.NewEncoder(r.Out)
		for i := 0; i < rv.Len(); i++ {
			if err := enc.Encode(rv.Index(i).Interface()); err != nil {
				return err
			}
		}
		return nil
	}
	return json.NewEncoder(r.Out).Encode(v)
}

func (r *Renderer) writeYAML(v any) error {
	// Round-trip through JSON so struct tags drive both representations.
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	var generic any
	if err := yaml.Unmarshal(data, &generic); err != nil {
		return err
	}
	out, err := yaml.Marshal(generic)
	if err != nil {
		return err
	}
	_, err = r.Out.Write(out)
	return err
}

func (r *Renderer) writeTSV(res *Result) error {
	if len(res.Rows) == 0 && res.Columns == nil {
		return r.writeJSON(res.Data)
	}
	if len(res.Columns) > 0 {
		fmt.Fprintln(r.Out, strings.Join(res.Columns, "\t"))
	}
	for _, row := range res.Rows {
		fmt.Fprintln(r.Out, strings.Join(row, "\t"))
	}
	return nil
}

func (r *Renderer) writeTemplate(v any) error {
	tpl, err := template.New("out").Funcs(template.FuncMap{
		"json":  func(x any) string { b, _ := json.Marshal(x); return string(b) },
		"upper": strings.ToUpper,
		"lower": strings.ToLower,
	}).Parse(r.Arg)
	if err != nil {
		return errs.Wrap(err, errs.CodeUsage, "invalid output template")
	}
	generic, err := toGeneric(v)
	if err != nil {
		return err
	}
	if err := tpl.Execute(r.Out, generic); err != nil {
		return errs.Wrap(err, errs.CodeUsage, "template execution failed")
	}
	fmt.Fprintln(r.Out)
	return nil
}

func (r *Renderer) writeJSONPath(v any) error {
	generic, err := toGeneric(v)
	if err != nil {
		return err
	}
	got, err := jsonPath(generic, r.Arg)
	if err != nil {
		return err
	}
	switch t := got.(type) {
	case string:
		fmt.Fprintln(r.Out, t)
	case []any:
		for _, x := range t {
			fmt.Fprintln(r.Out, scalar(x))
		}
	default:
		fmt.Fprintln(r.Out, scalar(got))
	}
	return nil
}

func scalar(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(t)
	default:
		b, _ := json.Marshal(t)
		return string(b)
	}
}

func toGeneric(v any) (any, error) {
	data, err := json.Marshal(v)
	if err != nil {
		return nil, errs.Wrap(err, errs.CodeInternal, "cannot serialise result")
	}
	var out any
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, errs.Wrap(err, errs.CodeInternal, "cannot decode result")
	}
	return out, nil
}

// jsonPath supports the dotted subset that covers CLI use: `.a.b`, `.a[0].b`,
// and `.a[*].b` to project a field across a list.
func jsonPath(v any, path string) (any, error) {
	segs := make([]string, 0, 4)
	p := strings.TrimPrefix(strings.TrimSpace(path), "$")
	for _, seg := range strings.Split(strings.TrimPrefix(p, "."), ".") {
		if seg != "" {
			segs = append(segs, seg)
		}
	}
	return walk(v, segs, path)
}

// walk resolves the remaining segments against cur.
//
// `[*]` recurses rather than returning, because the wildcard is the projection: `.items[*].name`
// has to answer with the names, not with the items. Returning the list there would silently ignore
// everything after the wildcard and print whole objects where a column was asked for.
func walk(cur any, segs []string, path string) (any, error) {
	for i, seg := range segs {
		name, idxs := parseSeg(seg)
		if name != "" {
			m, ok := cur.(map[string]any)
			if !ok {
				return nil, errs.New(errs.CodeUsage, "jsonpath %q: %q is not an object", path, name)
			}
			cur, ok = m[name]
			if !ok {
				return nil, errs.New(errs.CodeNotFound, "jsonpath %q: no field %q", path, name)
			}
		}
		for j, idx := range idxs {
			list, ok := cur.([]any)
			if !ok {
				return nil, errs.New(errs.CodeUsage, "jsonpath %q: not a list", path)
			}
			if idx == -1 {
				rest := segs[i+1:]
				if j+1 < len(idxs) {
					// A trailing index after the wildcard, e.g. `[*][0]`, is not
					// something this subset expresses.
					return nil, errs.New(errs.CodeUsage, "jsonpath %q: cannot index into a projection", path)
				}
				if len(rest) == 0 {
					return list, nil
				}
				out := make([]any, 0, len(list))
				for _, item := range list {
					got, err := walk(item, rest, path)
					if err != nil {
						return nil, err
					}
					out = append(out, got)
				}
				return out, nil
			}
			if idx < 0 || idx >= len(list) {
				return nil, errs.New(errs.CodeNotFound, "jsonpath %q: index %d out of range", path, idx)
			}
			cur = list[idx]
		}
	}
	return cur, nil
}

func parseSeg(seg string) (string, []int) {
	name := seg
	var idxs []int
	if i := strings.Index(seg, "["); i >= 0 {
		name = seg[:i]
		rest := seg[i:]
		for len(rest) > 0 && rest[0] == '[' {
			end := strings.Index(rest, "]")
			if end < 0 {
				break
			}
			inner := rest[1:end]
			if inner == "*" {
				idxs = append(idxs, -1)
			} else if n, err := strconv.Atoi(inner); err == nil {
				idxs = append(idxs, n)
			}
			rest = rest[end+1:]
		}
	}
	return name, idxs
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func nonNilActions(a []errs.Action) []errs.Action {
	if a == nil {
		return []errs.Action{}
	}
	return a
}
