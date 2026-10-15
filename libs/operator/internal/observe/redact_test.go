package observe

import (
	"context"
	"fmt"
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// A workload's argument list is where a token can sit in plain text. Discovery keeps the serving
// flags the plane reads and the names of the rest, and drops every other value.
func TestDiscoveryRedactsArgumentValues(t *testing.T) {
	got := redactedArgs([]string{
		"--model", "Qwen/Qwen3-32B",
		"--max-model-len=32768",
		"--hf-token", "hf_secret",
		"--api-key=sk-secret",
		"--enforce-eager",
		"positional-secret",
		"--tensor-parallel-size", "4",
	})
	want := []string{
		"--model", "Qwen/Qwen3-32B",
		"--max-model-len", "32768",
		"--hf-token",
		"--api-key",
		"--enforce-eager",
		"--tensor-parallel-size", "4",
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("redactedArgs = %q, want %q", got, want)
	}
}

func TestDiscoveryKeepsOnlyTheCommandsEntrypoint(t *testing.T) {
	cases := []struct {
		in, want []string
	}{
		{[]string{"python3", "-m", "dynamo.vllm"}, []string{"python3", "-m", "dynamo.vllm"}},
		{[]string{"/bin/sh", "-c", "HF_TOKEN=hf_secret vllm serve x"}, []string{"sh"}},
		{nil, []string{}},
	}
	for _, c := range cases {
		if got := commandShape(c.in); !reflect.DeepEqual(got, c.want) {
			t.Errorf("commandShape(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// pagedNodes serves nodes two to a page, the way an API server does under a limit.
type pagedNodes struct {
	client.Reader
	names []string
	calls int
}

func (p *pagedNodes) List(_ context.Context, list client.ObjectList, opts ...client.ListOption) error {
	p.calls++
	o := (&client.ListOptions{}).ApplyOptions(opts)
	if o.Limit == 0 {
		return fmt.Errorf("an unpaged LIST")
	}
	start := 0
	if o.Continue != "" {
		fmt.Sscanf(o.Continue, "%d", &start)
	}
	end := min(start+2, len(p.names))
	nodes := list.(*corev1.NodeList)
	for _, name := range p.names[start:end] {
		nodes.Items = append(nodes.Items, corev1.Node{ObjectMeta: metav1.ObjectMeta{Name: name}})
	}
	if end < len(p.names) {
		nodes.Continue = fmt.Sprint(end)
	}
	return nil
}

func TestListAllFollowsContinueTokens(t *testing.T) {
	reader := &pagedNodes{names: []string{"a", "b", "c", "d", "e"}}
	var nodes corev1.NodeList
	if err := listAll(context.Background(), reader, &nodes); err != nil {
		t.Fatal(err)
	}
	if len(nodes.Items) != 5 || nodes.Items[4].Name != "e" {
		t.Errorf("listed %d nodes, want all 5 across pages", len(nodes.Items))
	}
	if reader.calls != 3 {
		t.Errorf("made %d LISTs, want 3 pages", reader.calls)
	}
}
