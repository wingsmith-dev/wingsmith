package ui

import (
	"os"
	"strconv"
	"strings"
	"unicode"

	"golang.org/x/term"
)

// IsTerminal reports whether the file is attached to a terminal. It asks the
// kernel rather than checking for a character device, because /dev/null is a
// character device and is not a terminal.
func IsTerminal(f *os.File) bool {
	if f == nil {
		return false
	}
	return term.IsTerminal(int(f.Fd()))
}

// Width returns the usable terminal width, defaulting to 100 columns when the
// stream is not a terminal (which keeps piped output stable).
func Width(f *os.File) int {
	if v := os.Getenv("COLUMNS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 20 {
			return n
		}
	}
	if !IsTerminal(f) {
		return 100
	}
	if n, _, err := term.GetSize(int(f.Fd())); err == nil && n > 20 {
		return n
	}
	return 100
}

// DisplayWidth counts printable columns, ignoring ANSI escapes and treating
// wide runes as two cells.
func DisplayWidth(s string) int {
	w, inEsc := 0, false
	for _, r := range s {
		switch {
		case r == '\x1b':
			inEsc = true
		case inEsc:
			if r == 'm' || r == '\x07' {
				inEsc = false
			}
		case r == '\t':
			w += 4
		case unicode.IsControl(r):
		case isWide(r):
			w += 2
		default:
			w++
		}
	}
	return w
}

func isWide(r rune) bool {
	return (r >= 0x1100 && r <= 0x115F) ||
		(r >= 0x2E80 && r <= 0xA4CF) ||
		(r >= 0xAC00 && r <= 0xD7A3) ||
		(r >= 0xF900 && r <= 0xFAFF) ||
		(r >= 0xFF00 && r <= 0xFF60) ||
		(r >= 0x1F300 && r <= 0x1FAFF)
}

// Pad right-pads s to n display columns.
func Pad(s string, n int) string {
	d := n - DisplayWidth(s)
	if d <= 0 {
		return s
	}
	return s + strings.Repeat(" ", d)
}

// PadLeft left-pads s to n display columns.
func PadLeft(s string, n int) string {
	d := n - DisplayWidth(s)
	if d <= 0 {
		return s
	}
	return strings.Repeat(" ", d) + s
}

// Truncate shortens s to at most n display columns, adding an ellipsis.
func Truncate(s string, n int) string {
	if n <= 0 || DisplayWidth(s) <= n {
		return s
	}
	var b strings.Builder
	w := 0
	for _, r := range s {
		rw := 1
		if isWide(r) {
			rw = 2
		}
		if w+rw > n-1 {
			break
		}
		b.WriteRune(r)
		w += rw
	}
	return b.String() + "…"
}
