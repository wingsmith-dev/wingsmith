package devices

import (
	"bytes"
	"os"
	"testing"
)

// TestEmbeddedTableMatchesTheContract keeps the copy the binary carries honest. contract.json is a
// symlink to contract/devices.json, which go:embed will not follow, so the copy is checked instead.
func TestEmbeddedTableMatchesTheContract(t *testing.T) {
	contract, err := os.ReadFile("contract.json")
	if err != nil {
		t.Fatalf("read contract table: %v", err)
	}
	if !bytes.Equal(bytes.TrimSpace(contract), bytes.TrimSpace(tableJSON)) {
		t.Fatal("table.json has drifted from contract/devices.json; copy the contract file over it")
	}
}

func TestResolveAcrossDialects(t *testing.T) {
	table := Load()
	cases := []struct {
		dialect string
		product string
		want    string
	}{
		{DialectDCGM, "NVIDIA RTX PRO 6000 Blackwell Server Edition", "rtx-pro-6000-96gb-server-edition"},
		{DialectGFD, "NVIDIA-H100-80GB-HBM3", "h100-80gb-sxm"},
		{DialectDCGM, "NVIDIA H100 80GB HBM3", "h100-80gb-sxm"},
		{DialectGFD, "NVIDIA-A100-SXM4-80GB", "a100-80gb-sxm"},
		{DialectGFD, "nvidia-h100-pcie", "h100-80gb-pcie"},
		{DialectGFD, "NVIDIA-QUANTUM-9999", "quantum-9999"},
		{DialectDCGM, "Some Unlisted Card", "some-unlisted-card"},
	}
	for _, c := range cases {
		if got := table.Resolve(c.dialect, c.product); got != c.want {
			t.Errorf("Resolve(%q, %q) = %q, want %q", c.dialect, c.product, got, c.want)
		}
	}
}

func TestSeedCardCarriesItsMemoryAndFormFactor(t *testing.T) {
	class, ok := Load().Lookup(DialectDCGM, "NVIDIA RTX PRO 6000 Blackwell Server Edition")
	if !ok {
		t.Fatal("seed card is not in the table")
	}
	if class.MemoryMiB != 97249 || class.FormFactor != "server-edition" || class.Vendor != "nvidia" {
		t.Fatalf("seed card entry is wrong: %+v", class)
	}
}

func TestADialectDoesNotBorrowAnothersAliases(t *testing.T) {
	table := Load()
	if _, ok := table.Lookup("vllm", "NVIDIA H200"); ok {
		t.Fatal("an unknown dialect must not resolve")
	}
}

func TestNormalizeCollapsesSpellingDrift(t *testing.T) {
	cases := [][2]string{
		{"NVIDIA-H100-80GB-HBM3", "h100-80gb-hbm3"},
		{"NVIDIA H100 80GB HBM3", "h100-80gb-hbm3"},
		{"  NVIDIA   L40S  ", "l40s"},
		{"NVIDIA A100-PCIE-40GB", "a100-pcie-40gb"},
		{"", ""},
	}
	for _, c := range cases {
		if got := Normalize(c[0]); got != c[1] {
			t.Errorf("Normalize(%q) = %q, want %q", c[0], got, c[1])
		}
	}
}

func TestFallthroughIsWarnedOnce(t *testing.T) {
	table, err := Parse(tableJSON)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	table.Resolve(DialectGFD, "NVIDIA-Made-Up-1")
	table.Resolve(DialectGFD, "NVIDIA-Made-Up-1")
	table.Resolve(DialectDCGM, "NVIDIA Made Up 2")
	table.mu.Lock()
	defer table.mu.Unlock()
	if len(table.seen) != 2 {
		t.Fatalf("expected two deduped fallthroughs, got %d", len(table.seen))
	}
}
