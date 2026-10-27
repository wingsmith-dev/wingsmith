package serving

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	dto "github.com/prometheus/client_model/go"
)

// availability-2026.1, written out here instead of imported, because the
// outcome taxonomy exists so that the gateway observes and the objective
// interprets. If this ever has to move into the binary to make a test pass, the
// binary has started deciding what a failure is.
var (
	failureOutcomes = []string{"shed", "no_upstream", "upstream_error", "upstream_timeout", "internal_error"}
	ratioOutcomes   = append([]string{"ok"}, failureOutcomes...)
)

func gathered(t *testing.T, recorder *metrics.Recorder, name string) []*dto.Metric {
	t.Helper()
	all, err := recorder.Gatherer().Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, family := range all {
		if family.GetName() == name {
			return family.GetMetric()
		}
	}
	return nil
}

func seriesLabels(m *dto.Metric) map[string]string {
	out := make(map[string]string, len(m.GetLabel()))
	for _, pair := range m.GetLabel() {
		out[pair.GetName()] = pair.GetValue()
	}
	return out
}

// requests sums hangar_gateway_requests_total over series matching every label
// given, exactly as `sum by ()` over the same matcher would.
func requests(t *testing.T, recorder *metrics.Recorder, match map[string]string) float64 {
	t.Helper()
	total := 0.0
	for _, m := range gathered(t, recorder, "hangar_gateway_requests_total") {
		labels := seriesLabels(m)
		ok := true
		for name, want := range match {
			if labels[name] != want {
				ok = false
				break
			}
		}
		if ok {
			total += m.Counter.GetValue()
		}
	}
	return total
}

func sumOver(t *testing.T, recorder *metrics.Recorder, outcomes []string, extra map[string]string) float64 {
	t.Helper()
	total := 0.0
	for _, outcome := range outcomes {
		match := map[string]string{"outcome": outcome, "origin": "live"}
		for name, value := range extra {
			match[name] = value
		}
		total += requests(t, recorder, match)
	}
	return total
}

// failureRatio is the acceptance standard's number, computed the way the
// objective computes it.
func failureRatio(t *testing.T, recorder *metrics.Recorder) float64 {
	t.Helper()
	numerator := sumOver(t, recorder, failureOutcomes, nil)
	denominator := sumOver(t, recorder, ratioOutcomes, nil)
	if denominator == 0 {
		t.Fatal("the denominator is empty, which is an absent ratio and not a zero one")
	}
	return numerator / denominator * 100
}

// handlerUpstream builds a route whose upstream is an arbitrary handler, which
// is how a 429 with a declared meaning is produced without a limiter: whatever
// issues one says which kind it was through MarkOutcome.
func handlerUpstream(t *testing.T, id, name, deployment, cluster string, h http.Handler) routing.Route {
	t.Helper()
	upstream := routing.NewUpstream(cluster, 100, true, h)
	upstream.DeploymentID = deployment
	upstream.ClusterName = cluster
	route := routing.NewRoute(name, "weighted", upstream)
	route.ID = id
	return route
}

func proxyRoute(t *testing.T, id, name, deployment, cluster, address string) routing.Route {
	t.Helper()
	parsed, err := url.Parse(address)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return handlerUpstream(t, id, name, deployment, cluster, NewReverseProxy(parsed, nil, 0))
}

func post(t *testing.T, handler http.Handler, body string, headers ...string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	for i := 0; i+1 < len(headers); i += 2 {
		req.Header.Set(headers[i], headers[i+1])
	}
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	return rec
}

// TestTheStandardsCarveOutIsInTheLabelsAndNotInTheStatusCode is the reason this
// endpoint exists. Two 429s, one excused and one not, and no query over status
// codes can tell them apart.
func TestTheStandardsCarveOutIsInTheLabelsAndNotInTheStatusCode(t *testing.T) {
	recorder := metrics.New()

	overLimit := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// What an enforcing gateway does when an identified caller is over its
		// own declared limit. The caller was told no; nothing failed.
		MarkOutcome(r, metrics.OutcomeRateLimited)
		w.WriteHeader(http.StatusTooManyRequests)
	})
	shedding := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// What this hop does when it has no capacity to admit anyone. Nobody
		// was over anything; we failed the request.
		MarkOutcome(r, metrics.OutcomeShed)
		w.WriteHeader(http.StatusTooManyRequests)
	})
	fine := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: map[string]routing.Route{
		"limited": handlerUpstream(t, "ep_1", "chat", "dep_1", "ap", overLimit),
		"shed":    handlerUpstream(t, "ep_1", "chat", "dep_1", "ap", shedding),
		"chat":    handlerUpstream(t, "ep_1", "chat", "dep_1", "ap", fine),
	}}})

	for i := 0; i < 99; i++ {
		post(t, handler, `{"model":"chat"}`)
	}
	post(t, handler, `{"model":"limited"}`)
	post(t, handler, `{"model":"shed"}`)

	if got := requests(t, recorder, map[string]string{"status": "429"}); got != 2 {
		t.Fatalf("429s = %v, want 2", got)
	}
	if got := requests(t, recorder, map[string]string{"outcome": "rate_limited"}); got != 1 {
		t.Fatalf("rate_limited = %v, want 1", got)
	}
	if got := requests(t, recorder, map[string]string{"outcome": "shed"}); got != 1 {
		t.Fatalf("shed = %v, want 1", got)
	}

	// One of the two 429s is in the ratio and one is outside it entirely, not
	// in the numerator, and not in the denominator either. Excusing a failure
	// by leaving it in the denominator would quietly improve the number.
	if got := failureRatio(t, recorder); got != 1 {
		t.Fatalf("failure ratio = %v%%, want 1%%: one shed request in a hundred counted ones", got)
	}
	if got := sumOver(t, recorder, ratioOutcomes, nil); got != 100 {
		t.Fatalf("denominator = %v, want 100 out of the 101 requests: the rate-limited caller is outside the ratio on both sides", got)
	}

	// And the number a query keyed off the status code would have produced,
	// stated so the difference is on the record: both 429s counted as failures.
	byStatus := requests(t, recorder, map[string]string{"status": "429"}) / 100 * 100
	if byStatus == failureRatio(t, recorder) {
		t.Fatal("keying the failure rate off status=429 gave the same answer here, so this test is no longer testing anything")
	}
}

func TestEveryRefusalThisHopIssuesNamesItsOwnOutcome(t *testing.T) {
	unreachable := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	unreachable.Close()

	for _, tc := range []struct {
		name     string
		body     string
		routes   map[string]routing.Route
		resolver Resolver
		status   string
		outcome  string
		endpoint string
	}{
		{
			name:     "a model we do not serve is the caller's mistake",
			body:     `{"model":"nope"}`,
			status:   "404",
			outcome:  "client_error",
			endpoint: metrics.EndpointUnknown,
		},
		{
			name:     "a body that is not JSON is the caller's mistake",
			body:     `{`,
			status:   "400",
			outcome:  "client_error",
			endpoint: metrics.EndpointUnknown,
		},
		{
			name:     "a body with no model is the caller's mistake",
			body:     `{"messages":[]}`,
			status:   "400",
			outcome:  "client_error",
			endpoint: metrics.EndpointUnknown,
		},
		{
			name:     "an endpoint with nowhere to route is ours",
			body:     `{"model":"chat"}`,
			routes:   map[string]routing.Route{"chat": func() routing.Route { r := routing.NewRoute("chat", "weighted"); r.ID = "ep_1"; return r }()},
			status:   "503",
			outcome:  "no_upstream",
			endpoint: "ep_1",
		},
		{
			name:     "a resolver that broke is ours alone",
			body:     `{"model":"chat"}`,
			resolver: brokenResolver{},
			status:   "500",
			outcome:  "internal_error",
			endpoint: metrics.EndpointUnknown,
		},
		{
			name:     "a cluster we could not reach is ours",
			body:     `{"model":"chat"}`,
			routes:   map[string]routing.Route{"chat": proxyRoute(t, "ep_1", "chat", "dep_1", "ap", unreachable.URL)},
			status:   "502",
			outcome:  "upstream_error",
			endpoint: "ep_1",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			recorder := metrics.New()
			var resolver Resolver = stubResolver{routes: tc.routes}
			if tc.resolver != nil {
				resolver = tc.resolver
			}
			handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: resolver})

			post(t, handler, tc.body)

			match := map[string]string{"status": tc.status, "outcome": tc.outcome, "hangar_endpoint": tc.endpoint}
			if got := requests(t, recorder, match); got != 1 {
				t.Fatalf("%v = %v, want 1 (series present: %v)", match, got, allSeries(t, recorder))
			}
		})
	}
}

func TestAnUpstreamsOwnAnswerIsClassifiedByWhoseFaultItIs(t *testing.T) {
	for _, tc := range []struct {
		status  int
		outcome string
		reason  string
	}{
		{http.StatusOK, "ok", "delivered"},
		{http.StatusBadRequest, "client_error", "the request was wrong"},
		{http.StatusUnauthorized, "upstream_error", "our own mounted credential was refused"},
		{http.StatusForbidden, "upstream_error", "our own mounted credential was refused"},
		{http.StatusTooManyRequests, "shed", "the cluster refused, and the caller was over nothing"},
		{http.StatusInternalServerError, "upstream_error", "the cluster broke"},
	} {
		t.Run(fmt.Sprintf("%d is %s because %s", tc.status, tc.outcome, tc.reason), func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
			}))
			defer upstream.Close()

			recorder := metrics.New()
			handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: map[string]routing.Route{
				"chat": proxyRoute(t, "ep_1", "chat", "dep_1", "ap", upstream.URL),
			}}})

			post(t, handler, `{"model":"chat"}`)

			match := map[string]string{
				"outcome": tc.outcome, "hangar_endpoint": "ep_1", "endpoint_name": "chat",
				"deployment": "dep_1", "cluster": "ap", "origin": "live",
			}
			if got := requests(t, recorder, match); got != 1 {
				t.Fatalf("%v = %v, want 1 (series: %v)", match, got, allSeries(t, recorder))
			}
		})
	}
}

func TestAProxyErrorSeparatesATimeoutFromACallerHangingUp(t *testing.T) {
	for _, tc := range []struct {
		name string
		err  error
		want metrics.Outcome
	}{
		{"the caller went away", context.Canceled, metrics.OutcomeCanceled},
		{"the cluster ran out of time", timeoutError{}, metrics.OutcomeUpstreamTimeout},
		{"the deadline elapsed", context.DeadlineExceeded, metrics.OutcomeUpstreamTimeout},
		{"nothing was listening", errors.New("connection refused"), metrics.OutcomeUpstreamError},
	} {
		t.Run(tc.name, func(t *testing.T) {
			obs := &observation{start: time.Now()}
			req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
			req = req.WithContext(context.WithValue(req.Context(), observationKey{}, obs))

			markProxyError(req, tc.err)

			if got := obs.sample(http.StatusBadGateway, time.Second).Outcome; got != tc.want {
				t.Fatalf("outcome = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestARejectedCallerIsOutsideTheRatioRatherThanInsideTheNumerator(t *testing.T) {
	recorder := metrics.New()
	tenants := tenantsFor(t, plane.GatewayTenant{
		ID: "ten_x", Slug: "x", Enabled: true, Ingress: "public", Auth: "api_key", Host: "x.hangar.dev",
	})
	handler := New(Config{Metrics: recorder, Router: stubResolver{}, Tenants: tenants, Zone: ZonePublic})

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`))
	req.Host = "x.hangar.dev"
	handler.ServeHTTP(httptest.NewRecorder(), req)

	if got := requests(t, recorder, map[string]string{"status": "401", "outcome": "auth_rejected"}); got != 1 {
		t.Fatalf("auth_rejected = %v, want 1", got)
	}
	if got := sumOver(t, recorder, ratioOutcomes, nil); got != 0 {
		t.Fatalf("denominator = %v, want 0: a rejected credential is the caller's problem", got)
	}
}

func TestProbeTrafficNeedsBothTheHeaderAndTheToken(t *testing.T) {
	recorder := metrics.New()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	handler := New(Config{
		Zone:    ZoneInternal,
		Metrics: recorder,
		Probes:  NewProbeMarker("older-token, newer-token"),
		Router: stubResolver{routes: map[string]routing.Route{
			"chat": proxyRoute(t, "ep_1", "chat", "dep_1", "ap", upstream.URL),
		}},
	})

	post(t, handler, `{"model":"chat"}`, "X-Hangar-Origin", "probe", "X-Hangar-Probe-Token", "newer-token")
	post(t, handler, `{"model":"chat"}`, "X-Hangar-Origin", "probe", "X-Hangar-Probe-Token", "older-token")
	// A header anyone can set is a way for any caller to remove itself from a
	// denominator we quote to a buyer.
	post(t, handler, `{"model":"chat"}`, "X-Hangar-Origin", "probe")
	post(t, handler, `{"model":"chat"}`, "X-Hangar-Origin", "probe", "X-Hangar-Probe-Token", "guessed")
	post(t, handler, `{"model":"chat"}`)

	if got := requests(t, recorder, map[string]string{"origin": "probe"}); got != 2 {
		t.Fatalf("probe requests = %v, want 2: both values of a rotating pair", got)
	}
	if got := requests(t, recorder, map[string]string{"origin": "live"}); got != 3 {
		t.Fatalf("live requests = %v, want 3", got)
	}
}

func TestWithNoProbeTokenConfiguredEverythingIsLive(t *testing.T) {
	if NewProbeMarker("") != nil {
		t.Fatal("an unset probe token must not build a marker")
	}
	recorder := metrics.New()
	handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{}})
	post(t, handler, `{"model":"nope"}`, "X-Hangar-Origin", "probe")
	if got := requests(t, recorder, map[string]string{"origin": "live"}); got != 1 {
		t.Fatalf("live requests = %v, want 1", got)
	}
}

func TestTheKubeletProbeIsNobodysAvailability(t *testing.T) {
	recorder := metrics.New()
	handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz = %d", rec.Code)
	}
	if got := len(gathered(t, recorder, "hangar_gateway_requests_total")); got != 0 {
		t.Fatalf("healthz produced %d series, want none", got)
	}
}

func TestTheModelListIsCountedUnderTheCatalogLabel(t *testing.T) {
	recorder := metrics.New()
	handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: map[string]routing.Route{"chat": {}}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/models", nil))

	if got := requests(t, recorder, map[string]string{"hangar_endpoint": metrics.EndpointCatalog, "outcome": "ok"}); got != 1 {
		t.Fatalf("catalog requests = %v, want 1 (series: %v)", got, allSeries(t, recorder))
	}
}

func TestTimeToFirstTokenIsOnlyMeasuredWhenAClusterProducedTheBytes(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"id":"1"}`))
	}))
	defer upstream.Close()

	recorder := metrics.New()
	handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: map[string]routing.Route{
		"chat": proxyRoute(t, "ep_1", "chat", "dep_1", "ap", upstream.URL),
	}}})

	// Our own 404 has a first byte too, and observing it would report this
	// hop's error envelope as a very fast model.
	post(t, handler, `{"model":"nope"}`)
	if got := len(gathered(t, recorder, "hangar_gateway_ttft_seconds")); got != 0 {
		t.Fatalf("a locally written error produced %d TTFT series, want none", got)
	}

	post(t, handler, `{"model":"chat"}`)
	series := gathered(t, recorder, "hangar_gateway_ttft_seconds")
	if len(series) != 1 || series[0].Histogram.GetSampleCount() != 1 {
		t.Fatalf("TTFT series = %v, want one observation", series)
	}
	if labels := seriesLabels(series[0]); labels["deployment"] != "dep_1" || labels["cluster"] != "ap" {
		t.Fatalf("TTFT labels = %v, want the upstream it was measured against", labels)
	}
}

func TestTokensComeFromTheResponseTailAndStreamingStaysStreaming(t *testing.T) {
	const stream = "data: {\"choices\":[{\"delta\":{\"content\":\"hel\"}}]}\n\n" +
		"data: {\"choices\":[{\"delta\":{\"content\":\"lo\"}}]}\n\n" +
		"data: {\"choices\":[],\"usage\":{\"prompt_tokens\":7000,\"completion_tokens\":11,\"prompt_tokens_details\":{\"cached_tokens\":128}}}\n\n" +
		"data: [DONE]\n\n"

	for _, tc := range []struct {
		name     string
		body     string
		wantIn   float64
		wantOut  float64
		wantCach float64
		absent   bool
	}{
		{
			name:     "a completion carries its usage at the end",
			body:     `{"id":"1","choices":[{"message":{"content":"hello"}}],"usage":{"prompt_tokens":41,"completion_tokens":9,"prompt_tokens_details":{"cached_tokens":16}}}`,
			wantIn:   41,
			wantOut:  9,
			wantCach: 16,
		},
		{
			name:     "a stream carries it in the terminal chunk",
			body:     stream,
			wantIn:   7000,
			wantOut:  11,
			wantCach: 128,
		},
		{
			name:   "a stream that never asked for usage discloses none",
			body:   "data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\ndata: [DONE]\n\n",
			absent: true,
		},
		{
			name:   "an explicit null is not a disclosure",
			body:   `{"id":"1","usage":null}`,
			absent: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = w.Write([]byte(tc.body))
			}))
			defer upstream.Close()

			recorder := metrics.New()
			handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: map[string]routing.Route{
				"chat": proxyRoute(t, "ep_1", "chat", "dep_1", "ap", upstream.URL),
			}}})

			rec := post(t, handler, `{"model":"chat","stream":true}`)
			// The metrics wrapper sits on the write path, and a wrapper that
			// hides Flush turns a token-by-token stream into one long pause
			// followed by the whole answer: silently, and only under load.
			if !rec.Flushed {
				t.Fatal("the response was never flushed through the recorder")
			}
			if rec.Body.String() != tc.body {
				t.Fatalf("body was altered on the way through:\n got %q\nwant %q", rec.Body.String(), tc.body)
			}

			series := gathered(t, recorder, "hangar_gateway_tokens_total")
			if tc.absent {
				if len(series) != 0 {
					t.Fatalf("token series = %v, want none: absent, never zero", series)
				}
				return
			}
			counts := map[string]float64{}
			for _, m := range series {
				counts[seriesLabels(m)["direction"]] = m.Counter.GetValue()
			}
			if counts["input"] != tc.wantIn || counts["output"] != tc.wantOut || counts["cached_input"] != tc.wantCach {
				t.Fatalf("tokens = %v, want in=%v out=%v cached=%v", counts, tc.wantIn, tc.wantOut, tc.wantCach)
			}
		})
	}
}

func TestTheUsageWindowIsTheTailAndNotTheWholeBody(t *testing.T) {
	// Far more than the ring holds, with a decoy usage object at the front that
	// must have fallen out of the window by the end.
	body := `{"usage":{"prompt_tokens":1,"completion_tokens":1}}` +
		strings.Repeat("x", 4*usageTailBytes) +
		`{"usage":{"prompt_tokens":512,"completion_tokens":64}}`

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(body))
	}))
	defer upstream.Close()

	recorder := metrics.New()
	handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: map[string]routing.Route{
		"chat": proxyRoute(t, "ep_1", "chat", "dep_1", "ap", upstream.URL),
	}}})

	if rec := post(t, handler, `{"model":"chat"}`); rec.Body.Len() != len(body) {
		t.Fatalf("forwarded %d bytes, want %d", rec.Body.Len(), len(body))
	}

	counts := map[string]float64{}
	for _, m := range gathered(t, recorder, "hangar_gateway_tokens_total") {
		counts[seriesLabels(m)["direction"]] = m.Counter.GetValue()
	}
	if counts["input"] != 512 || counts["output"] != 64 {
		t.Fatalf("tokens = %v, want the last usage object in the response", counts)
	}
}

func TestInFlightIsReleasedWhateverHappened(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer upstream.Close()

	recorder := metrics.New()
	handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: map[string]routing.Route{
		"chat": proxyRoute(t, "ep_1", "chat", "dep_1", "ap", upstream.URL),
	}}})

	post(t, handler, `{"model":"chat"}`)

	for _, m := range gathered(t, recorder, "hangar_gateway_requests_in_flight") {
		if m.Gauge.GetValue() != 0 {
			t.Fatalf("in flight = %v after the request finished", m.Gauge.GetValue())
		}
	}
}

func TestTheHandlerWorksWithNoRecorderAtAll(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"chat": proxyRoute(t, "ep_1", "chat", "dep_1", "ap", upstream.URL),
	}}})

	if rec := post(t, handler, `{"model":"chat"}`); rec.Code != http.StatusOK {
		t.Fatalf("status = %d with no recorder configured", rec.Code)
	}
}

func allSeries(t *testing.T, recorder *metrics.Recorder) []map[string]string {
	t.Helper()
	out := []map[string]string{}
	for _, m := range gathered(t, recorder, "hangar_gateway_requests_total") {
		out = append(out, seriesLabels(m))
	}
	return out
}

type brokenResolver struct{}

func (brokenResolver) Resolve(string) (routing.Route, error) {
	return routing.Route{}, errors.New("the table is on fire")
}
func (brokenResolver) Names() []string                { return nil }
func (brokenResolver) NamesForTenant(string) []string { return nil }
func (brokenResolver) Revision() string               { return "" }

type timeoutError struct{}

func (timeoutError) Error() string   { return "i/o timeout" }
func (timeoutError) Timeout() bool   { return true }
func (timeoutError) Temporary() bool { return true }

var _ net.Error = timeoutError{}

// TestAReplayedBodyIsCounted drives the GetBody the transport calls when it
// sends a request again, and expects one replay on the request's series.
func TestAReplayedBodyIsCounted(t *testing.T) {
	recorder := metrics.New()
	replay := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.GetBody == nil {
			t.Error("the forwarded request is not replayable")
			return
		}
		body, err := r.GetBody()
		if err != nil {
			t.Errorf("GetBody: %v", err)
			return
		}
		_ = body.Close()
		w.WriteHeader(http.StatusOK)
	})
	route := routing.NewRoute("chat", "weighted", routing.NewUpstream("ap", 100, true, replay))
	handler := New(Config{Zone: ZoneInternal, Metrics: recorder,
		Router: stubResolver{routes: map[string]routing.Route{"chat": route}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`)))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	series := gathered(t, recorder, "hangar_gateway_upstream_replays_total")
	if len(series) != 1 || series[0].GetCounter().GetValue() != 1 {
		t.Fatalf("replays = %v, want one series at 1", series)
	}
	if labels := seriesLabels(series[0]); labels["endpoint_name"] != "chat" {
		t.Fatalf("labels = %v, want the request's endpoint", labels)
	}
}

// Trace context is the caller's to propagate: the gateway forwards W3C
// traceparent and tracestate untouched, so a trace started at the edge
// continues into the cluster.
func TestTraceContextReachesTheCluster(t *testing.T) {
	seen := make(chan http.Header, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen <- r.Header.Clone()
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(server.Close)
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{"chat": routeTo(t, server.URL, nil)}}})

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`))
	req.Header.Set("Traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
	req.Header.Set("Tracestate", "vendor=opaque")
	handler.ServeHTTP(httptest.NewRecorder(), req)

	got := <-seen
	if got.Get("Traceparent") != "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01" || got.Get("Tracestate") != "vendor=opaque" {
		t.Fatalf("upstream saw traceparent %q tracestate %q", got.Get("Traceparent"), got.Get("Tracestate"))
	}
}
