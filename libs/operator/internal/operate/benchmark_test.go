package operate

import (
	"testing"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
)

func framed(version, summary string) string {
	return "aiperf: starting profile\n" +
		"| metric | value |\n" +
		render.VersionLine + "\n" + version + "\n" +
		render.SummaryBegin + "\n" + summary + "\n" + render.SummaryEnd + "\n"
}

func TestExtractSummaryLiftsTheExportOutOfTheLog(t *testing.T) {
	summary, version := parseBenchmarkLog(framed("aiperf 0.4.1", `{"time_to_first_token":{"avg":182.4}}`))
	if string(summary) != `{"time_to_first_token":{"avg":182.4}}` {
		t.Errorf("summary is %q", string(summary))
	}
	if version != "0.4.1" {
		t.Errorf("version is %q", version)
	}
}

// A truncated export stored as a run's result is worse than an obvious gap: it reads as a
// measurement and is not one.
func TestExtractSummaryRefusesInvalidJSON(t *testing.T) {
	summary, _ := parseBenchmarkLog(framed("aiperf 0.4.1", `{"time_to_first_token":{"avg":18`))
	if summary != nil {
		t.Errorf("half an export was accepted: %q", string(summary))
	}
}

// The harness prints `null` when it wrote no export. That is a gap, not a result.
func TestExtractSummaryTreatsNullAsNoResult(t *testing.T) {
	if summary, _ := parseBenchmarkLog(framed("aiperf 0.4.1", "null")); summary != nil {
		t.Errorf("null was stored as a summary: %q", string(summary))
	}
}

// A job that died before printing anything must yield nothing rather than half of somebody
// else's output.
func TestExtractSummaryHandlesAnUnframedLog(t *testing.T) {
	summary, version := parseBenchmarkLog("Traceback (most recent call last):\n  ConnectionRefused\n")
	if summary != nil || version != "" {
		t.Errorf("got summary=%q version=%q from a crashed job", string(summary), version)
	}
}

// `aiperf --version` fails on an image that does not carry the subcommand, and the script prints
// `unknown` instead. That is what should be recorded — not an empty field that reads as "not asked".
func TestExtractVersionKeepsAnUnknownAnswer(t *testing.T) {
	if _, version := parseBenchmarkLog(framed("unknown", "null")); version != "unknown" {
		t.Errorf("version is %q", version)
	}
}

// A missing summary block with a version present must not walk forward into the next section and
// return a marker as a version.
func TestExtractVersionStopsAtTheSummaryMarker(t *testing.T) {
	logs := render.VersionLine + "\n" + render.SummaryBegin + "\nnull\n" + render.SummaryEnd + "\n"
	if _, version := parseBenchmarkLog(logs); version != "" {
		t.Errorf("version is %q; nothing was printed between the markers", version)
	}
}
