package cli

import (
	"fmt"
	"strings"
)

// unifiedDiff is a line diff of a and b in unified format with three lines of
// context, or "" when they are equal. Manifests are small, so a plain LCS table
// is enough.
func unifiedDiff(nameA, nameB, a, b string) string {
	if a == b {
		return ""
	}
	x, y := splitLines(a), splitLines(b)
	n, m := len(x), len(y)
	lcs := make([][]int, n+1)
	for i := range lcs {
		lcs[i] = make([]int, m+1)
	}
	for i := n - 1; i >= 0; i-- {
		for j := m - 1; j >= 0; j-- {
			if x[i] == y[j] {
				lcs[i][j] = lcs[i+1][j+1] + 1
			} else {
				lcs[i][j] = max(lcs[i+1][j], lcs[i][j+1])
			}
		}
	}
	type op struct {
		kind byte
		line string
		i, j int // 0-based positions in x and y before this op
	}
	var ops []op
	i, j := 0, 0
	for i < n || j < m {
		switch {
		case i < n && j < m && x[i] == y[j]:
			ops = append(ops, op{' ', x[i], i, j})
			i, j = i+1, j+1
		case i < n && (j == m || lcs[i+1][j] >= lcs[i][j+1]):
			ops = append(ops, op{'-', x[i], i, j})
			i++
		default:
			ops = append(ops, op{'+', y[j], i, j})
			j++
		}
	}

	const context = 3
	var out strings.Builder
	fmt.Fprintf(&out, "--- %s\n+++ %s\n", nameA, nameB)
	for k := 0; k < len(ops); {
		if ops[k].kind == ' ' {
			k++
			continue
		}
		start := max(k-context, 0)
		end := k
		for end < len(ops) {
			if ops[end].kind != ' ' {
				end++
				continue
			}
			run := end
			for run < len(ops) && ops[run].kind == ' ' {
				run++
			}
			if run == len(ops) || run-end > 2*context {
				end = min(end+context, len(ops))
				break
			}
			end = run
		}
		var lenA, lenB int
		for _, o := range ops[start:end] {
			if o.kind != '+' {
				lenA++
			}
			if o.kind != '-' {
				lenB++
			}
		}
		fmt.Fprintf(&out, "@@ -%s +%s @@\n", hunkRange(ops[start].i, lenA), hunkRange(ops[start].j, lenB))
		for _, o := range ops[start:end] {
			out.WriteByte(o.kind)
			out.WriteString(o.line)
			out.WriteByte('\n')
		}
		k = end
	}
	return out.String()
}

func hunkRange(start, length int) string {
	if length == 0 {
		return fmt.Sprintf("%d,0", start)
	}
	if length == 1 {
		return fmt.Sprintf("%d", start+1)
	}
	return fmt.Sprintf("%d,%d", start+1, length)
}

func splitLines(s string) []string {
	if s == "" {
		return nil
	}
	return strings.Split(strings.TrimSuffix(s, "\n"), "\n")
}
