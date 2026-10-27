package metrics

import (
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"
	"time"

	dto "github.com/prometheus/client_model/go"
)

// clock is a hand-wound clock, so the age of a table is asserted, not
// slept for.
type clock struct{ at time.Time }

func (c *clock) now() time.Time      { return c.at }
func (c *clock) add(d time.Duration) { c.at = c.at.Add(d) }

func testRecorder(t *testing.T) (*Recorder, *clock) {
	t.Helper()
	c := &clock{at: time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)}
	return New(WithClock(c.now)), c
}

func families(t *testing.T, r *Recorder) map[string]*dto.MetricFamily {
	t.Helper()
	gathered, err := r.Gatherer().Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	byName := make(map[string]*dto.MetricFamily, len(gathered))
	for _, family := range gathered {
		byName[family.GetName()] = family
	}
	return byName
}

func labelsOf(m *dto.Metric) map[string]string {
	out := make(map[string]string, len(m.GetLabel()))
	for _, pair := range m.GetLabel() {
		out[pair.GetName()] = pair.GetValue()
	}
	return out
}

func matches(m *dto.Metric, want map[string]string) bool {
	have := labelsOf(m)
	for name, value := range want {
		if have[name] != value {
			return false
		}
	}
	return true
}

// value sums every series of a family matching the labels, the way a PromQL
// sum by () would. A name with no series at all reports absent.
func value(t *testing.T, r *Recorder, name string, want map[string]string) (float64, bool) {
	t.Helper()
	family, ok := families(t, r)[name]
	if !ok {
		return 0, false
	}
	total, found := 0.0, false
	for _, m := range family.GetMetric() {
		if !matches(m, want) {
			continue
		}
		found = true
		switch {
		case m.Counter != nil:
			total += m.Counter.GetValue()
		case m.Gauge != nil:
			total += m.Gauge.GetValue()
		case m.Histogram != nil:
			total += float64(m.Histogram.GetSampleCount())
		}
	}
	return total, found
}

func mustValue(t *testing.T, r *Recorder, name string, want map[string]string) float64 {
	t.Helper()
	got, ok := value(t, r, name, want)
	if !ok {
		t.Fatalf("%s{%v} is absent, want a series", name, want)
	}
	return got
}

func TestTableAgeIsTimeSinceTheLastConfirmationNotTheLastChange(t *testing.T) {
	recorder, clock := testRecorder(t)

	recorder.TableApplied(2, []UpstreamSample{{Endpoint: "ep_1", Deployment: "dep_1", Cluster: "ap", Ready: true}})
	if age := mustValue(t, recorder, "hangar_gateway_routing_table_age_seconds", nil); age != 0 {
		t.Fatalf("age right after a load = %v, want 0", age)
	}

	clock.add(30 * time.Second)
	if age := mustValue(t, recorder, "hangar_gateway_routing_table_age_seconds", nil); age != 30 {
		t.Fatalf("age = %v, want 30", age)
	}

	// A 304 is the plane vouching for what is already loaded, and a table that
	// has not changed in a week is healthy. Age has to reset here or it would
	// report a stable fleet as a stale one.
	recorder.TableUnchanged()
	if age := mustValue(t, recorder, "hangar_gateway_routing_table_age_seconds", nil); age != 0 {
		t.Fatalf("age after a 304 = %v, want 0", age)
	}

	clock.add(45 * time.Second)
	if age := mustValue(t, recorder, "hangar_gateway_routing_table_age_seconds", nil); age != 45 {
		t.Fatalf("age = %v, want 45", age)
	}
	if confirmed := mustValue(t, recorder, "hangar_gateway_routing_table_confirmed_timestamp_seconds", nil); confirmed != float64(clock.at.Add(-45*time.Second).Unix()) {
		t.Fatalf("confirmed timestamp = %v, want the moment of the 304", confirmed)
	}
}

func TestAFailedRefreshLeavesTheAgeRunningAndTheGatewayServing(t *testing.T) {
	recorder, clock := testRecorder(t)
	recorder.TableApplied(1, nil)

	clock.add(60 * time.Second)
	recorder.TableRefreshFailed()

	// The failure must not look like a confirmation. This is the whole shape of
	// a plane outage from out here: the table keeps serving, the age keeps
	// climbing, and the success gauge is the thing that says why.
	if age := mustValue(t, recorder, "hangar_gateway_routing_table_age_seconds", nil); age != 60 {
		t.Fatalf("age after a failed refresh = %v, want it still climbing from the last success", age)
	}
	if ok := mustValue(t, recorder, "hangar_gateway_routing_table_refresh_success", nil); ok != 0 {
		t.Fatalf("refresh_success = %v, want 0", ok)
	}
	if errors := mustValue(t, recorder, "hangar_gateway_routing_table_refreshes_total", map[string]string{"result": "error"}); errors != 1 {
		t.Fatalf("error refreshes = %v, want 1", errors)
	}

	clock.add(30 * time.Second)
	recorder.TableUnchanged()
	if ok := mustValue(t, recorder, "hangar_gateway_routing_table_refresh_success", nil); ok != 1 {
		t.Fatalf("refresh_success after recovery = %v, want 1", ok)
	}
	if age := mustValue(t, recorder, "hangar_gateway_routing_table_age_seconds", nil); age != 0 {
		t.Fatalf("age after recovery = %v, want 0", age)
	}
}

func TestRefreshResultsAreCountedFromZeroRatherThanAppearingLater(t *testing.T) {
	recorder, _ := testRecorder(t)

	for _, result := range []string{"applied", "unchanged", "error"} {
		if _, ok := value(t, recorder, "hangar_gateway_routing_table_refreshes_total", map[string]string{"result": result}); !ok {
			t.Fatalf("result=%q has no series before anything happened, so an alert on it has a hole to subtract from", result)
		}
	}

	recorder.TableApplied(3, nil)
	recorder.TableUnchanged()
	recorder.TableUnchanged()
	if applied := mustValue(t, recorder, "hangar_gateway_routing_table_refreshes_total", map[string]string{"result": "applied"}); applied != 1 {
		t.Fatalf("applied = %v, want 1", applied)
	}
	if unchanged := mustValue(t, recorder, "hangar_gateway_routing_table_refreshes_total", map[string]string{"result": "unchanged"}); unchanged != 2 {
		t.Fatalf("unchanged = %v, want 2", unchanged)
	}
	if endpoints := mustValue(t, recorder, "hangar_gateway_routing_table_endpoints", nil); endpoints != 3 {
		t.Fatalf("endpoints = %v, want 3", endpoints)
	}
}

func TestUpstreamGaugesForgetWhatLeftTheTable(t *testing.T) {
	recorder, _ := testRecorder(t)

	recorder.TableApplied(2, []UpstreamSample{
		{Endpoint: "ep_1", EndpointName: "chat", Deployment: "dep_1", Cluster: "ap", Ready: true},
		{Endpoint: "ep_2", EndpointName: "code", Deployment: "dep_2", Cluster: "us", Ready: false},
	})
	if up := mustValue(t, recorder, "hangar_gateway_upstream_up", map[string]string{"deployment": "dep_1"}); up != 1 {
		t.Fatalf("ready upstream = %v, want 1", up)
	}
	if up := mustValue(t, recorder, "hangar_gateway_upstream_up", map[string]string{"deployment": "dep_2"}); up != 0 {
		t.Fatalf("unready upstream = %v, want 0", up)
	}

	recorder.TableApplied(1, []UpstreamSample{
		{Endpoint: "ep_1", EndpointName: "chat", Deployment: "dep_1", Cluster: "ap", Ready: true},
	})
	// A gauge frozen at its last value is worse than no gauge: it reports a
	// deployment that no longer exists as healthy, forever.
	if _, ok := value(t, recorder, "hangar_gateway_upstream_up", map[string]string{"deployment": "dep_2"}); ok {
		t.Fatal("a deployment that left the table is still reporting")
	}
}

func TestTokensAreAbsentRatherThanZeroWhenNoUsageArrived(t *testing.T) {
	recorder, _ := testRecorder(t)

	recorder.Observe(Sample{
		Endpoint: "ep_1", EndpointName: "chat", Deployment: "dep_1", Cluster: "ap",
		Status: 200, Outcome: OutcomeOK, Duration: time.Second,
	})
	if _, ok := value(t, recorder, "hangar_gateway_tokens_total", nil); ok {
		t.Fatal("a response with no usage object produced token series, and a zero there is a claim we cannot make")
	}

	recorder.Observe(Sample{
		Endpoint: "ep_1", EndpointName: "chat", Deployment: "dep_1", Cluster: "ap",
		Status: 200, Outcome: OutcomeOK, Duration: time.Second,
		HaveUsage: true, InputTokens: 900, OutputTokens: 120, CachedInputTokens: 300,
	})
	if in := mustValue(t, recorder, "hangar_gateway_tokens_total", map[string]string{"direction": "input"}); in != 900 {
		t.Fatalf("input tokens = %v, want 900", in)
	}
	if out := mustValue(t, recorder, "hangar_gateway_tokens_total", map[string]string{"direction": "output"}); out != 120 {
		t.Fatalf("output tokens = %v, want 120", out)
	}
	if cached := mustValue(t, recorder, "hangar_gateway_tokens_total", map[string]string{"direction": "cached_input"}); cached != 300 {
		t.Fatalf("cached input tokens = %v, want 300", cached)
	}
}

func TestARequestThatReachedNoClusterKeepsItsLabelsAsEmptyStrings(t *testing.T) {
	recorder, _ := testRecorder(t)

	recorder.Observe(Sample{
		Endpoint: EndpointUnknown, Status: 404, Outcome: OutcomeClientError, Duration: time.Millisecond,
	})

	family := families(t, recorder)["hangar_gateway_requests_total"]
	if family == nil || len(family.GetMetric()) != 1 {
		t.Fatalf("want exactly one series, got %v", family)
	}
	labels := labelsOf(family.GetMetric()[0])
	for _, name := range []string{"deployment", "cluster", "endpoint_name"} {
		if _, present := labels[name]; !present {
			t.Fatalf("%s is missing from the series; the requests no cluster could have seen are the ones that matter most, and an absent label makes them queryable only by a negation over an unbounded set", name)
		}
		if labels[name] != "" {
			t.Fatalf("%s = %q, want the empty string", name, labels[name])
		}
	}
	if labels["origin"] != string(OriginLive) {
		t.Fatalf("origin = %q, want it defaulted to live", labels["origin"])
	}
}

// TestTheLabelSetIsClosed is the cardinality bill, enforced. A caller id, an
// API key, a client address or a raw model string on any of these series is
// both unbounded and the thing this hop's posture exists to keep out.
func TestTheLabelSetIsClosed(t *testing.T) {
	recorder, _ := testRecorder(t)
	recorder.TableApplied(1, []UpstreamSample{{Endpoint: "ep_1", EndpointName: "chat", Deployment: "dep_1", Cluster: "ap", Ready: true}})
	release := recorder.InFlight("ep_1", "chat", OriginLive, "dep_1", "ap")
	recorder.Observe(Sample{
		Endpoint: "ep_1", EndpointName: "chat", Origin: OriginLive, Deployment: "dep_1", Cluster: "ap",
		Status: 200, Outcome: OutcomeOK, Duration: time.Second,
		TTFT: 100 * time.Millisecond, HaveTTFT: true,
		HaveUsage: true, InputTokens: 10, OutputTokens: 20,
	})

	want := map[string][]string{
		"hangar_gateway_requests_total":                {"cluster", "deployment", "endpoint_name", "hangar_endpoint", "origin", "outcome", "status"},
		"hangar_gateway_request_duration_seconds":      {"cluster", "deployment", "endpoint_name", "hangar_endpoint", "origin"},
		"hangar_gateway_ttft_seconds":                  {"cluster", "deployment", "endpoint_name", "hangar_endpoint", "origin"},
		"hangar_gateway_ttft_by_input_seconds":         {"endpoint_name", "hangar_endpoint", "input_bucket", "origin"},
		"hangar_gateway_tokens_total":                  {"cluster", "deployment", "direction", "endpoint_name", "hangar_endpoint", "origin"},
		"hangar_gateway_requests_in_flight":            {"cluster", "deployment", "endpoint_name", "hangar_endpoint", "origin"},
		"hangar_gateway_upstream_up":                   {"cluster", "deployment", "endpoint_name", "hangar_endpoint", "origin"},
		"hangar_gateway_routing_table_refreshes_total": {"result"},
	}

	byName := families(t, recorder)
	for name, expected := range want {
		family, ok := byName[name]
		if !ok {
			t.Fatalf("%s was not exposed", name)
		}
		got := make([]string, 0, len(expected))
		for _, pair := range family.GetMetric()[0].GetLabel() {
			got = append(got, pair.GetName())
		}
		sort.Strings(got)
		if strings.Join(got, ",") != strings.Join(expected, ",") {
			t.Fatalf("%s labels = %v, want exactly %v", name, got, expected)
		}
	}
	release()
	if flight := mustValue(t, recorder, "hangar_gateway_requests_in_flight", nil); flight != 0 {
		t.Fatalf("in flight after release = %v, want 0", flight)
	}
}

func TestTTFTIsBandedByTheRequestsOwnPromptLength(t *testing.T) {
	recorder, _ := testRecorder(t)

	recorder.Observe(Sample{
		Endpoint: "ep_1", Status: 200, Outcome: OutcomeOK, Duration: 2 * time.Second,
		TTFT: 300 * time.Millisecond, HaveTTFT: true,
		HaveUsage: true, InputTokens: 40000,
	})
	// A streaming caller that never asked for usage has no prompt length to
	// band by, and the gap goes on the panel, not into a band.
	recorder.Observe(Sample{
		Endpoint: "ep_1", Status: 200, Outcome: OutcomeOK, Duration: 2 * time.Second,
		TTFT: 300 * time.Millisecond, HaveTTFT: true,
	})

	if n := mustValue(t, recorder, "hangar_gateway_ttft_by_input_seconds", map[string]string{"input_bucket": "32k_64k"}); n != 1 {
		t.Fatalf("32k_64k observations = %v, want 1", n)
	}
	if n := mustValue(t, recorder, "hangar_gateway_ttft_by_input_seconds", map[string]string{"input_bucket": InputBucketUnknown}); n != 1 {
		t.Fatalf("unknown observations = %v, want 1", n)
	}
}

func TestInputBucketBoundaries(t *testing.T) {
	for _, tc := range []struct {
		tokens int64
		want   string
	}{
		{0, "lt6k"},
		{6*1024 - 1, "lt6k"},
		{6 * 1024, "6k_16k"},
		{16 * 1024, "16k_32k"},
		{32 * 1024, "32k_64k"},
		{64 * 1024, "64k_128k"},
		{128 * 1024, "128k_256k"},
		{256 * 1024, "gt256k"},
		{4_000_000, "gt256k"},
	} {
		if got := InputBucket(tc.tokens, true); got != tc.want {
			t.Fatalf("InputBucket(%d) = %q, want %q", tc.tokens, got, tc.want)
		}
	}
	if got := InputBucket(1234, false); got != InputBucketUnknown {
		t.Fatalf("undisclosed prompt length = %q, want %q", got, InputBucketUnknown)
	}
}

func TestOutcomeFromStatusBlamesTheRightParty(t *testing.T) {
	for _, tc := range []struct {
		status int
		want   Outcome
	}{
		{200, OutcomeOK},
		{204, OutcomeOK},
		{400, OutcomeClientError},
		{404, OutcomeClientError},
		{422, OutcomeClientError},
		// Our own mounted credential was refused. Blaming the caller for our
		// expired secret is a wrong answer that looks right until somebody looks.
		{401, OutcomeUpstreamError},
		{403, OutcomeUpstreamError},
		// An upstream's 429 is not attributable to the caller, so it is shed
		// and it is a failure.
		{429, OutcomeShed},
		{500, OutcomeUpstreamError},
		{503, OutcomeUpstreamError},
	} {
		if got := OutcomeFromStatus(tc.status); got != tc.want {
			t.Fatalf("OutcomeFromStatus(%d) = %q, want %q", tc.status, got, tc.want)
		}
	}
}

func TestStatusLabelStaysBounded(t *testing.T) {
	if got := StatusLabel(429); got != "429" {
		t.Fatalf("allowlisted code = %q", got)
	}
	// An upstream can answer with anything, and anything is an unbounded axis
	// on the busiest series in the process.
	for status, want := range map[int]string{599: "500", 498: "400", 399: "300", 299: "200", 0: "0"} {
		if got := StatusLabel(status); got != want {
			t.Fatalf("StatusLabel(%d) = %q, want %q", status, got, want)
		}
	}
}

func TestHandlerServesTheExpositionFormat(t *testing.T) {
	recorder, _ := testRecorder(t)
	recorder.TableApplied(1, nil)
	recorder.Observe(Sample{Endpoint: "ep_1", Status: 200, Outcome: OutcomeOK, Duration: time.Second})

	rec := httptest.NewRecorder()
	recorder.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	body := rec.Body.String()
	for _, name := range []string{
		"hangar_gateway_requests_total",
		"hangar_gateway_request_duration_seconds_bucket",
		"hangar_gateway_routing_table_age_seconds",
		"hangar_gateway_routing_table_refresh_success",
	} {
		if !strings.Contains(body, name) {
			t.Fatalf("%s is not in the exposition", name)
		}
	}
}

func TestBuildInfoCarriesTheVersion(t *testing.T) {
	r, _ := testRecorder(t)
	r.BuildInfo("v1.2.3")
	if got := mustValue(t, r, "hangar_gateway_build_info", map[string]string{"version": "v1.2.3"}); got != 1 {
		t.Fatalf("build info = %v, want 1", got)
	}
}
