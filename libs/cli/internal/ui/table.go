package ui

import (
	"fmt"
	"io"
	"strings"
)

// Align controls per-column justification.
type Align int

const (
	AlignLeft Align = iota
	AlignRight
)

// Column describes one table column.
type Column struct {
	Title string
	Align Align
	// Wide marks a column that only renders under `-o wide`.
	Wide bool
}

// Table renders borderless, gutter-separated tables. No box drawing: dense
// output that greps and eyeballs equally well.
type Table struct {
	style   Style
	cols    []Column
	rows    [][]string
	wide    bool
	maxCols int
}

// NewTable creates a table bound to a style and terminal width.
func NewTable(style Style, width int, wide bool, cols ...Column) *Table {
	return &Table{style: style, cols: cols, wide: wide, maxCols: width}
}

// Row appends a row. Extra cells are ignored; missing cells render empty.
func (t *Table) Row(cells ...string) { t.rows = append(t.rows, cells) }

func (t *Table) visible() []int {
	var idx []int
	for i, c := range t.cols {
		if c.Wide && !t.wide {
			continue
		}
		idx = append(idx, i)
	}
	return idx
}

// Render writes the table, sizing columns to content and shrinking the widest
// column first when the terminal is too narrow.
func (t *Table) Render(w io.Writer) {
	idx := t.visible()
	if len(idx) == 0 {
		return
	}
	widths := make([]int, len(idx))
	for i, ci := range idx {
		widths[i] = DisplayWidth(t.cols[ci].Title)
	}
	for _, r := range t.rows {
		for i, ci := range idx {
			if ci < len(r) {
				if n := DisplayWidth(r[ci]); n > widths[i] {
					widths[i] = n
				}
			}
		}
	}
	const gutter = 2
	total := (len(idx)-1)*gutter + sum(widths)
	for total > t.maxCols && t.maxCols > 0 {
		w := widest(widths)
		if widths[w] <= 8 {
			break
		}
		widths[w] -= total - t.maxCols
		if widths[w] < 8 {
			widths[w] = 8
		}
		total = (len(idx)-1)*gutter + sum(widths)
	}

	var head []string
	for i, ci := range idx {
		title := strings.ToUpper(t.cols[ci].Title)
		if t.cols[ci].Align == AlignRight {
			head = append(head, PadLeft(title, widths[i]))
		} else {
			head = append(head, Pad(title, widths[i]))
		}
	}
	fmt.Fprintln(w, t.style.Dim(strings.TrimRight(strings.Join(head, strings.Repeat(" ", gutter)), " ")))

	for _, r := range t.rows {
		var cells []string
		for i, ci := range idx {
			v := ""
			if ci < len(r) {
				v = r[ci]
			}
			v = Truncate(v, widths[i])
			if t.cols[ci].Align == AlignRight {
				cells = append(cells, PadLeft(v, widths[i]))
			} else {
				cells = append(cells, Pad(v, widths[i]))
			}
		}
		fmt.Fprintln(w, strings.TrimRight(strings.Join(cells, strings.Repeat(" ", gutter)), " "))
	}
}

func sum(v []int) int {
	n := 0
	for _, x := range v {
		n += x
	}
	return n
}

func widest(v []int) int {
	best := 0
	for i, x := range v {
		if x > v[best] {
			best = i
		}
		_ = x
	}
	return best
}

// KV renders an aligned key/value detail block, the standard `show` layout.
type KV struct {
	style Style
	keys  []string
	vals  []string
}

// NewKV creates a detail block.
func NewKV(style Style) *KV { return &KV{style: style} }

// Add appends a row; empty values are dropped so detail views stay dense.
func (k *KV) Add(key, val string) {
	if strings.TrimSpace(val) == "" {
		return
	}
	k.keys = append(k.keys, key)
	k.vals = append(k.vals, val)
}

// Addf appends a formatted row.
func (k *KV) Addf(key, format string, args ...any) { k.Add(key, fmt.Sprintf(format, args...)) }

// Render writes the block with two-space indentation.
func (k *KV) Render(w io.Writer) {
	max := 0
	for _, s := range k.keys {
		if n := DisplayWidth(s); n > max {
			max = n
		}
	}
	for i := range k.keys {
		fmt.Fprintf(w, "  %s  %s\n", k.style.Dim(Pad(k.keys[i], max)), k.vals[i])
	}
}

// Sparkline renders a series as a single line of block characters.
func Sparkline(style Style, values []float64) string {
	if len(values) == 0 {
		return ""
	}
	ramp := []rune("▁▂▃▄▅▆▇█")
	if !style.Unicode {
		ramp = []rune(".:-=+*#%")
	}
	min, max := values[0], values[0]
	for _, v := range values {
		if v < min {
			min = v
		}
		if v > max {
			max = v
		}
	}
	span := max - min
	var b strings.Builder
	for _, v := range values {
		i := 0
		if span > 0 {
			i = int((v - min) / span * float64(len(ramp)-1))
		}
		b.WriteRune(ramp[i])
	}
	return style.Cyan(b.String())
}

// Bar renders a proportional bar of the given cell width.
func Bar(style Style, fraction float64, width int) string {
	if fraction < 0 {
		fraction = 0
	}
	if fraction > 1 {
		fraction = 1
	}
	filled := int(fraction * float64(width))
	full, empty := "█", "░"
	if !style.Unicode {
		full, empty = "#", "."
	}
	return style.Cyan(strings.Repeat(full, filled)) + style.Dim(strings.Repeat(empty, width-filled))
}
