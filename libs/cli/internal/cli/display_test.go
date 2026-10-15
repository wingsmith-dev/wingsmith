package cli

import "testing"

func TestCountsAgreeWithTheirNumber(t *testing.T) {
	for n, want := range map[int]string{0: "0 replicas", 1: "1 replica", 2: "2 replicas"} {
		if got := countWord(n, "replica"); got != want {
			t.Errorf("countWord(%d) = %q, want %q", n, got, want)
		}
	}
	if got := clusterNoun().count(1); got != "1 cluster" {
		t.Errorf("count(1) = %q", got)
	}
	if got := clusterNoun().count(3); got != "3 clusters" {
		t.Errorf("count(3) = %q", got)
	}
}
