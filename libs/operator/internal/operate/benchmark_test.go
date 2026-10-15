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

// Read-only with no allowlist, the chart's default, refuses every kind.
func TestAReadOnlyOperatorWithNoAllowlistRefusesEverything(t *testing.T) {
	e := readOnlyExecutor()
	for _, kind := range []string{"drain", "cordon", "benchmark"} {
		_, err := e.dispatch(t.Context(), wire.OperationDesired{Kind: kind})
		if err == nil || !strings.Contains(err.Error(), "read-only") {
			t.Errorf("a read-only operator performed %q: %v", kind, err)
		}
	}
}

// The plane may not own what runs here and may still be asked to measure it.
func TestAReadOnlyOperatorPerformsAnAllowedKind(t *testing.T) {
	e := readOnlyExecutor("benchmark")
	_, err := e.dispatch(t.Context(), wire.OperationDesired{Kind: "benchmark"})
	if err != nil && strings.Contains(err.Error(), "read-only") {
		t.Errorf("benchmark was allowed and still refused as read-only: %v", err)
	}
}

// Allowing one kind must not allow the rest, and the refusal names what is allowed.
func TestAllowingOneKindDoesNotAllowTheOthers(t *testing.T) {
	e := readOnlyExecutor("benchmark")
	for _, kind := range []string{"drain", "cordon"} {
		_, err := e.dispatch(t.Context(), wire.OperationDesired{Kind: kind})
		if err == nil || !strings.Contains(err.Error(), "performs only benchmark") {
			t.Errorf("%q was performed by a read-only operator allowing only benchmark: %v", kind, err)
		}
	}
}

// A managed operator performs every kind; the allowlist is the read-only exception only. The
// chart grants RBAC on the same rule, so a second gate here would refuse what it permitted.
func TestTheAllowlistDoesNotConstrainAManagedOperator(t *testing.T) {
	e := New(&Executor{Log: discardLogger(), ReadOnly: false})
	for _, kind := range []string{"drain", "cordon", "benchmark"} {
		_, err := e.dispatch(t.Context(), wire.OperationDesired{Kind: kind})
		if err != nil && strings.Contains(err.Error(), "read-only") {
			t.Errorf("a managed operator refused %q as read-only: %v", kind, err)
		}
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
	endpoint, err := e.frontendEndpoint(context.Background(), "dep_01J9X2")
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
	_, err := e.frontendEndpoint(context.Background(), "dep_01J9X2")
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
	_, err := e.frontendEndpoint(context.Background(), "dep_01J9X2")
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

// deploymentFrontendService is what Dynamo derives from a frontend this operator rendered: the
// component Service, carrying the pod template's ownership labels.
func deploymentFrontendService(name, namespace, deploymentID string, port int32) *corev1.Service {
	service := frontendService(name, namespace, port)
	service.Labels = map[string]string{deploymentIDLabel: deploymentID}
	return service
}

// One frontend per model means a run can aim at the frontend that actually serves the model under
// test, rather than at a process shared with every other model on the cluster — whose queueing
// would be measured as if it were this model's.
func TestBenchmarkPicksTheFrontendForTheDeploymentUnderTest(t *testing.T) {
	e := &Executor{
		Client: fake.NewClientBuilder().WithObjects(
			deploymentFrontendService("kimi-k3-frontend", "dynamo", "dep_kimi", 8000),
			deploymentFrontendService("qwen3-frontend", "dynamo", "dep_qwen", 8000),
		).Build(),
		Render: render.Options{Namespace: "dynamo"},
	}
	endpoint, err := e.frontendEndpoint(context.Background(), "dep_kimi")
	if err != nil {
		t.Fatalf("lookup: %v", err)
	}
	if endpoint != "http://kimi-k3-frontend.dynamo.svc.cluster.local:8000" {
		t.Errorf("wrong endpoint: %s", endpoint)
	}
}

// A deployment whose frontend is labelled must not be answered with the shared one standing beside
// it: that is the measurement through somebody else's process this change exists to stop.
func TestBenchmarkPrefersTheLabelledFrontendOverTheSharedOne(t *testing.T) {
	e := &Executor{
		Client: fake.NewClientBuilder().WithObjects(
			frontendService("inference-frontend", "dynamo", 8000),
			deploymentFrontendService("kimi-k3-frontend", "dynamo", "dep_kimi", 8000),
		).Build(),
		Render: render.Options{Namespace: "dynamo"},
	}
	endpoint, err := e.frontendEndpoint(context.Background(), "dep_kimi")
	if err != nil {
		t.Fatalf("lookup: %v", err)
	}
	if !strings.Contains(endpoint, "kimi-k3-frontend") {
		t.Errorf("the run was aimed at %s, not at the deployment's own frontend", endpoint)
	}
}

// The migration case, and the reason the fallback is not merely a nicety: a cluster part-way
// through has one shared frontend and some models with their own, and a deployment that has not
// moved is still served by the shared one.
func TestBenchmarkFallsBackToTheOneUnlabelledFrontend(t *testing.T) {
	e := &Executor{
		Client: fake.NewClientBuilder().WithObjects(
			frontendService("inference-frontend", "dynamo", 8000),
			deploymentFrontendService("kimi-k3-frontend", "dynamo", "dep_kimi", 8000),
		).Build(),
		Render: render.Options{Namespace: "dynamo"},
	}
	endpoint, err := e.frontendEndpoint(context.Background(), "dep_qwen")
	if err != nil {
		t.Fatalf("lookup: %v", err)
	}
	if !strings.Contains(endpoint, "inference-frontend") {
		t.Errorf("the run was aimed at %s, not at the shared frontend", endpoint)
	}
}

// Dynamo copies the pod template's labels onto the Service it derives, and a Service selecting on
// the deployment id can only reach that deployment's pods. Either says the same thing.
func TestBenchmarkReadsTheDeploymentIdOffTheSelectorToo(t *testing.T) {
	labelled := frontendService("kimi-k3-frontend", "dynamo", 8000)
	labelled.Spec.Selector[deploymentIDLabel] = "dep_kimi"
	e := &Executor{
		Client: fake.NewClientBuilder().WithObjects(
			labelled,
			deploymentFrontendService("qwen3-frontend", "dynamo", "dep_qwen", 8000),
		).Build(),
		Render: render.Options{Namespace: "dynamo"},
	}
	endpoint, err := e.frontendEndpoint(context.Background(), "dep_kimi")
	if err != nil {
		t.Fatalf("lookup: %v", err)
	}
	if !strings.Contains(endpoint, "kimi-k3-frontend") {
		t.Errorf("wrong endpoint: %s", endpoint)
	}
}

// The refusal survives for the case it was written for and no wider: several frontends, none of
// them this deployment's, where picking one would attribute a measurement to a deployment the load
// never reached.
func TestBenchmarkStillRefusesWhenNoFrontendIsThisDeploymentsAndTwoAreShared(t *testing.T) {
	e := &Executor{
		Client: fake.NewClientBuilder().WithObjects(
			frontendService("one-frontend", "dynamo", 8000),
			frontendService("two-frontend", "dynamo", 8000),
			deploymentFrontendService("kimi-k3-frontend", "dynamo", "dep_kimi", 8000),
		).Build(),
		Render: render.Options{Namespace: "dynamo"},
	}
	_, err := e.frontendEndpoint(context.Background(), "dep_qwen")
	if err == nil || !strings.Contains(err.Error(), "HANGAR_INFERENCE_ENDPOINT") {
		t.Fatalf("the error does not name the way out: %v", err)
	}
	if !strings.Contains(err.Error(), "dep_qwen") {
		t.Errorf("the refusal does not say which deployment it could not place: %v", err)
	}
}

// A deployment whose frontend was rendered twice — two graphs, one id — is the one case where the
// label answers nothing, and guessing between them is the same mistake as before.
func TestBenchmarkRefusesTwoFrontendsWearingTheSameDeploymentId(t *testing.T) {
	e := &Executor{
		Client: fake.NewClientBuilder().WithObjects(
			deploymentFrontendService("kimi-a-frontend", "dynamo", "dep_kimi", 8000),
			deploymentFrontendService("kimi-b-frontend", "dynamo", "dep_kimi", 8000),
		).Build(),
		Render: render.Options{Namespace: "dynamo"},
	}
	_, err := e.frontendEndpoint(context.Background(), "dep_kimi")
	if err == nil || !strings.Contains(err.Error(), "kimi-a-frontend") {
		t.Fatalf("the error does not name what it found: %v", err)
	}
}
