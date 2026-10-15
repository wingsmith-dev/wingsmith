package cli

import (
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// The root help lists the verbs that change something, noun by noun. Every
// one it names has to exist, and every noun with such a verb has to be named,
// so the help never promises more or less than the binary does.
func TestRootHelpListsTheRealWriteVerbs(t *testing.T) {
	root := newRootCmd(&App{})
	listed := map[string][]string{}
	for _, line := range strings.Split(root.Long, "\n") {
		if !strings.HasPrefix(line, "  ") {
			continue
		}
		fields := strings.Fields(line)
		noun, rest := fields[0], strings.Join(fields[1:], " ")
		if i := strings.Index(rest, "("); i >= 0 {
			rest = rest[:i] + rest[strings.Index(rest, ")")+1:]
		}
		for _, v := range strings.Split(rest, ",") {
			listed[noun] = append(listed[noun], strings.TrimSpace(v))
		}
	}
	if len(listed) == 0 {
		t.Fatal("found no verb table in the root help")
	}
	for noun, verbs := range listed {
		cmd := sub(root, noun)
		if cmd == nil {
			t.Errorf("the root help names %q, which is not a command", noun)
			continue
		}
		for _, v := range verbs {
			if sub(cmd, v) == nil {
				t.Errorf("the root help lists %s %s, which does not exist", noun, v)
			}
		}
	}
	readOnly := map[string]bool{"ls": true, "get": true, "events": true, "harnesses": true,
		"verdict": true, "rules": true, "resolved": true, "bands": true}
	for _, noun := range root.Commands() {
		if noun.GroupID == "meta" || noun.GroupID == "" {
			continue
		}
		for _, verb := range noun.Commands() {
			if readOnly[verb.Name()] {
				continue
			}
			found := false
			for _, v := range listed[noun.Name()] {
				found = found || v == verb.Name()
			}
			if !found {
				t.Errorf("%s %s changes something but the root help does not list it", noun.Name(), verb.Name())
			}
		}
	}
}

func sub(cmd *cobra.Command, name string) *cobra.Command {
	for _, c := range cmd.Commands() {
		if c.Name() == name {
			return c
		}
	}
	return nil
}
