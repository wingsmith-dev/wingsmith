package cli

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// examples pulls every runnable line out of a command's Example block. A line
// ending in a backslash continues on the next. Lines that pipe, set the
// environment, or carry a placeholder (…, <x>) are illustrations, not commands.
func examples(cmd *cobra.Command) [][]string {
	var out [][]string
	var cur string
	for _, line := range strings.Split(cmd.Example, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasSuffix(line, `\`) {
			cur += strings.TrimSuffix(line, `\`) + " "
			continue
		}
		cur += line
		full := strings.TrimSpace(cur)
		cur = ""
		if !strings.HasPrefix(full, "hgr ") || strings.ContainsAny(full, "…<>|$=") && !onlyFlagEquals(full) {
			continue
		}
		out = append(out, strings.Fields(full)[1:])
	}
	return out
}

// onlyFlagEquals allows `--flag=value` and KEY=VALUE arguments while still
// skipping environment prefixes and pipes.
func onlyFlagEquals(line string) bool {
	return !strings.ContainsAny(line, "…<>|$")
}

func walk(cmd *cobra.Command, visit func(*cobra.Command)) {
	visit(cmd)
	for _, c := range cmd.Commands() {
		walk(c, visit)
	}
}

// preconditions are what an example assumes happened first, keyed by the start
// of the example. The seed is one fixed state, and an example of undoing
// something needs the something.
var preconditions = map[string][]string{
	"serving-quota create chat": {"serving-quota", "rm", "chat", "--yes"},
	"discovered restore":        {"discovered", "rm", "llama-70b-vllm"},
}

// unrunnable are examples the fake cannot carry out, and why.
var unrunnable = map[string]string{
	"benchmark run chat --param concurrency=64 --wait":                           "the fake never executes a benchmark, so --wait polls until it times out",
	"benchmark run chat --param concurrency=64 --param request_count=500 --wait": "the fake never executes a benchmark, so --wait polls until it times out",
}

// Every example in the help runs against the fake. An example that fails is
// the first thing a new user copies and the first thing that tells them the
// tool is broken.
func TestEveryExampleRuns(t *testing.T) {
	root := newRootCmd(&App{})
	var ran int
	walk(root, func(cmd *cobra.Command) {
		for _, args := range examples(cmd) {
			t.Run(strings.Join(args, " "), func(t *testing.T) {
				t.Setenv("HANGAR_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
				t.Setenv("HANGAR_ADMIN_PASSWORD", "example")
				line := strings.Join(args, " ")
				if why, ok := unrunnable[line]; ok {
					t.Skip(why)
				}
				for prefix, setup := range preconditions {
					if strings.HasPrefix(line, prefix) {
						if _, errOut, code := run(t, setup...); code != 0 {
							t.Fatalf("precondition %v: exit %d %s", setup, code, errOut)
						}
					}
				}
				args := append(args, "--yes", "-o", "json")
				out, errOut, code := run(t, args...)
				if code != 0 {
					t.Errorf("exit %d\n%s%s", code, out, errOut)
				}
			})
			ran++
		}
	})
	if ran < 20 {
		t.Fatalf("only %d examples found; the parser is not reading the help", ran)
	}
}
