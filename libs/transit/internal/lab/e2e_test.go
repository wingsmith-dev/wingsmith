package lab

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/transit/internal/load"
	"github.com/hangar-dev/hangar/libs/transit/internal/upstream"
)

// TestSyntheticPathEndToEnd builds the real gateway and agent from this
// checkout and runs one short stage through them against the fake plane. It is
// the test that proves the fake still serves what the gateway needs to start.
// It compiles two binaries, so it runs only when HANGAR_TRANSIT_E2E is set;
// `just transit-smoke` is the same check from the command line.
func TestSyntheticPathEndToEnd(t *testing.T) {
	if os.Getenv("HANGAR_TRANSIT_E2E") == "" || testing.Short() {
		t.Skip("set HANGAR_TRANSIT_E2E=1 to build the gateway and agent and run the path")
	}
	repo, err := FindRepo(".")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	report, err := RunSynthetic(ctx, Options{
		Repo:     repo,
		Agents:   1,
		Upstream: upstream.Profile{TTFT: 20 * time.Millisecond, ITL: time.Millisecond, Tokens: 8},
		Stages:   []load.Stage{{Concurrency: 4, Duration: 2 * time.Second, Drain: 5 * time.Second}},
		Stream:   true,
		SLO:      DefaultSLO(),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := report.Err(); err != nil {
		t.Fatalf("%v; bottleneck: %s", err, report.Bottleneck)
	}
	stage := report.Stages[0]
	if stage.Tunnels == nil {
		t.Fatalf("could not read the gateway's tunnel gauge; %d configured", stage.ConfiguredTunnels)
	}
	if *stage.Tunnels != stage.ConfiguredTunnels {
		t.Fatalf("gateway held %d tunnels of %d configured", *stage.Tunnels, stage.ConfiguredTunnels)
	}
}
