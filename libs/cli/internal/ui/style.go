// Package ui owns everything the human eye sees: colour, symbols, tables,
// progress, and prompts. Nothing here ever writes to stdout by itself — the
// caller supplies the stream so data and narration stay separated.
package ui

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

// ColorLevel describes how much colour the attached terminal can render.
type ColorLevel int

const (
	ColorNone ColorLevel = iota
	Color16
	Color256
	ColorTrue
)

// Style renders text with the brand palette, degrading to whatever the
// terminal supports.
type Style struct {
	Level   ColorLevel
	Unicode bool
	Links   bool
}

type rgb struct{ r, g, b uint8 }

// Brand palette, from the Console concept terminal.
var (
	cDim     = rgb{0x8a, 0x95, 0xa3}
	cText    = rgb{0xe8, 0xec, 0xf1}
	cGreen   = rgb{0x54, 0xe0, 0x8a}
	cCyan    = rgb{0x3d, 0xd7, 0xd0}
	cAmber   = rgb{0xff, 0xb0, 0x00}
	cRed     = rgb{0xff, 0x6b, 0x6b}
	cMagenta = rgb{0xc4, 0x8a, 0xff}
)

var ansi16 = map[rgb]int{
	cDim: 90, cText: 97, cGreen: 92, cCyan: 96, cAmber: 93, cRed: 91, cMagenta: 95,
}

var ansi256 = map[rgb]int{
	cDim: 245, cText: 255, cGreen: 84, cCyan: 80, cAmber: 214, cRed: 203, cMagenta: 141,
}

func (s Style) paint(c rgb, text string, bold bool) string {
	if s.Level == ColorNone || text == "" {
		return text
	}
	var seq string
	switch s.Level {
	case ColorTrue:
		seq = fmt.Sprintf("38;2;%d;%d;%d", c.r, c.g, c.b)
	case Color256:
		seq = "38;5;" + strconv.Itoa(ansi256[c])
	default:
		seq = strconv.Itoa(ansi16[c])
	}
	if bold {
		seq = "1;" + seq
	}
	return "\x1b[" + seq + "m" + text + "\x1b[0m"
}

func (s Style) Dim(t string) string      { return s.paint(cDim, t, false) }
func (s Style) Text(t string) string     { return s.paint(cText, t, false) }
func (s Style) Bold(t string) string     { return s.paint(cText, t, true) }
func (s Style) Green(t string) string    { return s.paint(cGreen, t, false) }
func (s Style) Cyan(t string) string     { return s.paint(cCyan, t, false) }
func (s Style) Amber(t string) string    { return s.paint(cAmber, t, false) }
func (s Style) Red(t string) string      { return s.paint(cRed, t, false) }
func (s Style) Magenta(t string) string  { return s.paint(cMagenta, t, false) }
func (s Style) Headline(t string) string { return s.paint(cAmber, t, true) }
func (s Style) ID(t string) string       { return s.paint(cCyan, t, false) }
func (s Style) Underline(t string) string {
	if s.Level == ColorNone {
		return t
	}
	return "\x1b[4m" + t + "\x1b[24m"
}

// Symbols, with ASCII fallbacks for terminals without a UTF-8 locale.
func (s Style) sym(uni, ascii string) string {
	if s.Unicode {
		return uni
	}
	return ascii
}

func (s Style) Tick() string  { return s.Green(s.sym("✓", "OK")) }
func (s Style) Cross() string { return s.Red(s.sym("✗", "XX")) }
func (s Style) Arrow() string { return s.Dim(s.sym("→", "->")) }
func (s Style) Bang() string  { return s.Amber(s.sym("!", "!!")) }
func (s Style) Hook() string  { return s.Dim(s.sym("↳", ">")) }
func (s Style) Dot() string   { return s.sym("·", "-") }
func (s Style) Bullet() string {
	return s.Dim(s.sym("•", "*"))
}

// Link renders an OSC-8 hyperlink where supported, falling back to plain text.
func (s Style) Link(label, url string) string {
	if !s.Links || url == "" {
		return label
	}
	return "\x1b]8;;" + url + "\x07" + s.Underline(s.Cyan(label)) + "\x1b]8;;\x07"
}

// StatusColor maps a resource status to its palette entry.
func (s Style) StatusColor(status string) string {
	switch strings.ToLower(status) {
	case "running", "ready", "active", "succeeded", "complete", "completed", "healthy", "published", "available":
		return s.Green(status)
	case "pending", "provisioning", "starting", "pulling", "scheduling", "queued", "scaling":
		return s.Amber(status)
	case "stopping", "stopped", "cancelled", "canceled", "idle", "scaled-to-zero", "unpublished":
		return s.Dim(status)
	case "failed", "error", "evicted", "unhealthy", "degraded":
		return s.Red(status)
	default:
		return status
	}
}

// DetectStyle resolves terminal capabilities for the given stream.
//
// FORCE_COLOR (or HGR_FORCE_COLOR) overrides the TTY check, which is what CI
// log viewers, `script`-less demo captures, and recorded terminal sessions
// need. An explicit --no-color still wins over it.
func DetectStyle(f *os.File, forceNoColor bool) Style {
	st := Style{Level: ColorNone, Unicode: unicodeOK(), Links: false}
	if forceNoColor {
		return st
	}
	if os.Getenv("NO_COLOR") != "" || os.Getenv("HGR_NO_COLOR") != "" || os.Getenv("TERM") == "dumb" {
		return st
	}
	if !IsTerminal(f) && !forcedColor() {
		return st
	}
	switch {
	case strings.Contains(os.Getenv("COLORTERM"), "truecolor"), strings.Contains(os.Getenv("COLORTERM"), "24bit"):
		st.Level = ColorTrue
	case strings.Contains(os.Getenv("TERM"), "256"):
		st.Level = Color256
	default:
		st.Level = Color16
	}
	// Terminals known to honour OSC-8.
	tp := os.Getenv("TERM_PROGRAM")
	st.Links = tp == "iTerm.app" || tp == "WezTerm" || tp == "ghostty" || tp == "vscode" || os.Getenv("KITTY_WINDOW_ID") != ""
	return st
}

func forcedColor() bool {
	for _, k := range []string{"HGR_FORCE_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE"} {
		if v := os.Getenv(k); v != "" && v != "0" && v != "false" {
			return true
		}
	}
	return false
}

func unicodeOK() bool {
	for _, k := range []string{"LC_ALL", "LC_CTYPE", "LANG"} {
		v := strings.ToUpper(os.Getenv(k))
		if v != "" {
			return strings.Contains(v, "UTF-8") || strings.Contains(v, "UTF8")
		}
	}
	return true
}
