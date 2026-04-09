package operate

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

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

// --------------------------------------------------------------------- read-only, and its exceptions

func readOnlyExecutor(allowed ...string) *Executor {
	return New(&Executor{Log: discardLogger(), ReadOnly: true, Allowed: allowed})
}

// The default is unchanged by the split. A cluster that was refusing everything before this
// existed must still refuse everything after it, or the change quietly granted it more than it had.
func TestAReadOnlyOperatorWithNoAllowlistStillRefusesEverything(t *testing.T) {
	e := readOnlyExecutor()
	for _, kind := range []string{"drain", "cordon", "benchmark"} {
		if _, err := e.dispatch(t.Context(), wire.OperationDesired{Kind: kind}); err == nil {
			t.Errorf("a read-only operator performed %q", kind)
		}
	}
}

// The case the split exists for: the plane may not own what runs here, and may still be asked to
// measure it.
func TestAReadOnlyOperatorPerformsAnAllowedKind(t *testing.T) {
	e := readOnlyExecutor("benchmark")
	_, err := e.dispatch(t.Context(), wire.OperationDesired{Kind: "benchmark"})
	if err != nil && strings.Contains(err.Error(), "read-only") {
		t.Errorf("benchmark was allowed and still refused as read-only: %v", err)
	}
}

// Allowing one kind must not allow the rest. A benchmark is additive and disposable; a drain on a
// cluster this plane does not manage is destructive, and a boolean would have conflated them.
func TestAllowingOneKindDoesNotAllowTheOthers(t *testing.T) {
	e := readOnlyExecutor("benchmark")
	for _, kind := range []string{"drain", "cordon"} {
		_, err := e.dispatch(t.Context(), wire.OperationDesired{Kind: kind})
		if err == nil || !strings.Contains(err.Error(), "read-only") {
			t.Errorf("%q was performed by a read-only operator allowing only benchmark: %v", kind, err)
		}
	}
}

// A refusal has to say what this operator would have done instead, because the alternative is an
// operator that looks broken rather than configured.
func TestARefusalNamesWhatIsAllowed(t *testing.T) {
	e := readOnlyExecutor("benchmark")
	_, err := e.dispatch(t.Context(), wire.OperationDesired{Kind: "drain"})
	if err == nil || !strings.Contains(err.Error(), "benchmark") {
		t.Errorf("the refusal does not name what is allowed: %v", err)
	}
}

// An operator that is not read-only performs every kind it implements. A second gate there would
// be a second meaning for one flag, which is the thing this split undid.
func TestTheAllowlistDoesNotConstrainAWritingOperator(t *testing.T) {
	e := New(&Executor{Log: discardLogger(), ReadOnly: false, Allowed: []string{"benchmark"}})
	_, err := e.dispatch(t.Context(), wire.OperationDesired{Kind: "cordon"})
	if err != nil && strings.Contains(err.Error(), "read-only") {
		t.Errorf("a writing operator was gated by the read-only allowlist: %v", err)
	}
}

// The endpoint used to be configuration with a default no cluster answered to, which failed inside
// the load generator as a DNS error. Asked for it, the operator finds the frontend the cluster
// actually runs, whatever it happens to be called.
func TestBenchmarkFindsTheFrontendTheClusterActuallyRuns(t *testing.T) {
	e := &Executor{
		Client: fake.NewClientBuilder().WithObjects(
			frontendService("qwen3-8-27b-agg-frontend", "dynamo", 8000),
			&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "nats", Namespace: "dynamo"}},
		).Build(),
		Render: render.Options{Namespace: "dynamo"},
	}
	endpoint, err := e.frontendEndpoint(context.Background())
	if err != nil {
		t.Fatalf("lookup: %v", err)
	}
	if endpoint != "http://qwen3-8-27b-agg-frontend.dynamo.svc.cluster.local:8000" {
		t.Errorf("wrong endpoint: %s", endpoint)
	}
}

// A cluster with nothing to benchmark against must say so in terms that name the setting, not
// leave the load generator to fail on a hostname minutes later.
func TestBenchmarkSaysSoWhenThereIsNoFrontend(t *testing.T) {
	e := &Executor{
		Client: fake.NewClientBuilder().Build(),
		Render: render.Options{Namespace: "dynamo"},
	}
	_, err := e.frontendEndpoint(context.Background())
	if err == nil || !strings.Contains(err.Error(), "HANGAR_INFERENCE_ENDPOINT") {
		t.Errorf("the error does not name the way out: %v", err)
	}
}

// Two frontends is an error and not a guess: picking one would attribute a measurement to a
// deployment the load never reached.
func TestBenchmarkRefusesToGuessBetweenTwoFrontends(t *testing.T) {
	e := &Executor{
		Client: fake.NewClientBuilder().WithObjects(
			frontendService("one-frontend", "dynamo", 8000),
			frontendService("two-frontend", "dynamo", 8000),
		).Build(),
		Render: render.Options{Namespace: "dynamo"},
	}
	_, err := e.frontendEndpoint(context.Background())
	if err == nil || !strings.Contains(err.Error(), "one-frontend") {
		t.Errorf("the error does not name what it found: %v", err)
	}
}

func frontendService(name, namespace string, port int32) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{frontendComponentLabel: "Frontend"},
			Ports:    []corev1.ServicePort{{Name: "http", Port: port}},
		},
	}
}
