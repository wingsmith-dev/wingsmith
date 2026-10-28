package reconcile

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/render"
)

// fakeFrontend answers for every pod at once, which is what the cluster case looks like: the
// frontends of one graph are replicas of each other and a model registers on all of them or none.
type fakeFrontend struct {
	models []string
	err    error
	asked  int
}

func (f *fakeFrontend) Serving(context.Context, string) ([]string, error) {
	f.asked++
	if f.err != nil {
		return nil, f.err
	}
	return f.models, nil
}

// addressed gives frontend pods the IP the probe is aimed at. A pod with no address has not been
// admitted anywhere yet, and asking it anything is a question with no recipient.
func addressed(pods []*corev1.Pod) []*corev1.Pod {
	for i, pod := range pods {
		pod.Status.PodIP = "10.0.0." + string(rune('1'+i))
	}
	return pods
}

func observedRolloutProbing(
	t *testing.T,
	opts render.Options,
	probe FrontendModels,
	pods ...*corev1.Pod,
) rollout {
	t.Helper()
	objects := make([]client.Object, 0, len(pods))
	for _, pod := range pods {
		objects = append(objects, pod)
	}
	r := &SyncedResourceReconciler{
		Client:    fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithObjects(objects...).Build(),
		Scheme:    clientgoscheme.Scheme,
		Log:       slog.Default(),
		Namespace: "wingsmith-system",
		Render:    opts,
		ClusterID: "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
		Frontend:  probe,
	}
	out, err := r.observeRollout(context.Background(), testDeploymentDesired())
	if err != nil {
		t.Fatalf("observe: %v", err)
	}
	return out
}

// The failure this check exists for, and the one every other signal in the cluster calls healthy.
//
// A frontend registers a model by resolving the card the worker published, and a card it cannot
// resolve leaves it listening, probing green and serving nothing. Every worker is ready, the graph
// reports `successful`, the agent forwards to it and the gateway prefers it, and each request is
// answered 404 by the frontend itself. Nothing above this line can see that, because everything
// above this line is counting pods.
func TestAReadyFrontendThatDoesNotServeTheModelIsNotRunning(t *testing.T) {
	probe := &fakeFrontend{models: []string{}}
	out := observedRolloutProbing(t, frontendRender(), probe,
		append(readyPods(2), addressed(frontendPods(2))...)...)

	if out.ready != 2 || out.desired != 2 {
		t.Fatalf("counts = %d of %d; the question must not move the model's replica counts",
			out.ready, out.desired)
	}
	if out.phase() != phaseDegraded {
		t.Errorf("phase = %q, want degraded: nothing can reach this model", out.phase())
	}
	if out.reason() != reasonModelNotRegistered {
		t.Errorf("reason = %q, want %q", out.reason(), reasonModelNotRegistered)
	}
	if !strings.Contains(out.message(), "qwen3.8-27b") {
		t.Errorf("message does not name the model nothing serves: %q", out.message())
	}
}

// The ordinary case. A frontend that lists the model says exactly what its counts already said,
// and the check has to cost nothing here or it will be turned off.
func TestAFrontendServingTheModelReadsAsItAlwaysDid(t *testing.T) {
	probe := &fakeFrontend{models: []string{"qwen3.8-27b"}}
	out := observedRolloutProbing(t, frontendRender(), probe,
		append(readyPods(2), addressed(frontendPods(2))...)...)

	if out.phase() != phaseRunning || out.reason() != "Serving" {
		t.Errorf("phase = %q, reason = %q", out.phase(), out.reason())
	}
	if probe.asked == 0 {
		t.Error("no frontend was asked anything")
	}
}

// Under one Dynamo namespace every frontend discovers every worker, so a frontend listing models
// is not evidence about this one. The list has to be searched for this deployment's own model, or
// the check passes on a cluster where one model registered and this one did not, which is the
// blast radius ADR 0013 leaves open and the exact shape this must not be blind to.
func TestAnotherModelOnTheFrontendIsNotThisModel(t *testing.T) {
	probe := &fakeFrontend{models: []string{"openai/gpt-oss-20b", "google/gemma-4-26b-a4b-it"}}
	out := observedRolloutProbing(t, frontendRender(), probe,
		append(readyPods(2), addressed(frontendPods(2))...)...)

	if out.reason() != reasonModelNotRegistered {
		t.Errorf("reason = %q; a busy frontend that never registered this model is not serving it",
			out.reason())
	}
}

// A question that could not be put is not a negative answer, and the direction of that mistake is
// the whole reason the probe returns two values. The plane withdraws a deployment it is told is
// not running, so a NetworkPolicy, a restarting pod or an operator with no route to the workload
// namespace would take a serving fleet out of the routing table: a check that causes the outage
// it was written to reveal.
func TestAFrontendThatCannotBeAskedIsTakenAtItsWord(t *testing.T) {
	probe := &fakeFrontend{err: errors.New("dial tcp 10.0.0.1:8000: i/o timeout")}
	out := observedRolloutProbing(t, frontendRender(), probe,
		append(readyPods(2), addressed(frontendPods(2))...)...)

	if out.phase() != phaseRunning || out.reason() != "Serving" {
		t.Errorf("phase = %q, reason = %q; an unreachable frontend must read as it did before",
			out.phase(), out.reason())
	}
}

// A pod with no address has not been admitted anywhere and cannot be asked, so it keeps the
// answer its own readiness gives. Same restraint as the unreachable case, one step earlier.
func TestAFrontendWithNoAddressIsNotAsked(t *testing.T) {
	probe := &fakeFrontend{models: []string{}}
	out := observedRolloutProbing(t, frontendRender(), probe,
		append(readyPods(2), frontendPods(2)...)...)

	if probe.asked != 0 {
		t.Errorf("a pod with no address was asked %d times", probe.asked)
	}
	if out.phase() != phaseRunning {
		t.Errorf("phase = %q", out.phase())
	}
}

// Left nil, nothing is asked and this loop behaves exactly as it did before the question existed.
// That is what keeps the check out of every test that is about something else.
func TestNoProbeAsksNothing(t *testing.T) {
	out := observedRollout(t, frontendRender(),
		append(readyPods(2), addressed(frontendPods(2))...)...)

	if out.phase() != phaseRunning || out.reason() != "Serving" {
		t.Errorf("phase = %q, reason = %q", out.phase(), out.reason())
	}
}

// The wire the probe actually reads. `/v1/models` is the OpenAI list shape and the ids are what
// the gateway routes on, so a parser that took the wrong field would report every frontend as
// serving nothing on a fleet that is fine.
func TestTheProbeReadsTheOpenAIModelList(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path != "/v1/models" {
			t.Errorf("asked for %q", req.URL.Path)
		}
		_, _ = w.Write([]byte(`{"object":"list","data":[{"id":"openai/gpt-oss-120b","object":"model"}]}`))
	}))
	defer server.Close()

	probe := NewHTTPFrontendModels()
	host := strings.TrimPrefix(server.URL, "http://")
	ids, err := probe.Serving(context.Background(), "")
	if err == nil && len(ids) > 0 {
		t.Fatal("an empty address must not resolve to anything")
	}

	ids, err = probe.servingAt(context.Background(), "http://"+host+"/v1/models")
	if err != nil {
		t.Fatalf("serving: %v", err)
	}
	if len(ids) != 1 || ids[0] != "openai/gpt-oss-120b" {
		t.Errorf("ids = %v", ids)
	}
}

// An empty list is an answer, not an error. It is the exact reading a frontend gives while its
// registration is failing, so collapsing it into an error would restore the silence.
func TestAnEmptyModelListIsAnAnswer(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"object":"list","data":[]}`))
	}))
	defer server.Close()

	ids, err := NewHTTPFrontendModels().servingAt(context.Background(), server.URL+"/v1/models")
	if err != nil {
		t.Fatalf("serving: %v", err)
	}
	if len(ids) != 0 {
		t.Errorf("ids = %v", ids)
	}
}
