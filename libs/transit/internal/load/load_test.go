package load

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	neturl "net/url"
	"strings"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/transit/internal/upstream"
)

func frontend(t *testing.T, profile upstream.Profile) string {
	t.Helper()
	srv := httptest.NewServer(upstream.New(profile).Handler())
	t.Cleanup(srv.Close)
	return srv.URL + "/v1/chat/completions"
}

func TestRunLetsInFlightRequestsFinishAfterTheHold(t *testing.T) {
	url := frontend(t, upstream.Profile{TTFT: 300 * time.Millisecond, ITL: time.Millisecond, Tokens: 4})
	stage := Stage{Concurrency: 2, Duration: 50 * time.Millisecond, Drain: 5 * time.Second}

	outcomes := Run(context.Background(), NewClient(2), Request{URL: url, Model: "m", Stream: true}, stage)
	if len(outcomes) != 2 {
		t.Fatalf("outcomes = %d, want one per worker", len(outcomes))
	}
	sample := Summarise(stage, outcomes)
	if sample.Succeeded != 2 || sample.Censored != 0 || sample.Faults != nil {
		t.Fatalf("sample = %+v, want both requests drained to success", sample)
	}
	if sample.TTFTP50 < 300*time.Millisecond {
		t.Fatalf("ttft p50 = %s, want at least the frontend's 300ms", sample.TTFTP50)
	}
	if sample.Elapsed < 300*time.Millisecond {
		t.Fatalf("elapsed = %s, want the drain counted", sample.Elapsed)
	}
}

func TestRunCensorsWhatOutlivesTheDrain(t *testing.T) {
	url := frontend(t, upstream.Profile{TTFT: 5 * time.Second, Tokens: 1})
	stage := Stage{Concurrency: 3, Duration: 20 * time.Millisecond, Drain: 150 * time.Millisecond}

	outcomes := Run(context.Background(), NewClient(3), Request{URL: url, Model: "m", Stream: true}, stage)
	if len(outcomes) != 3 {
		t.Fatalf("outcomes = %d, want 3: a cut-off request is kept, not dropped", len(outcomes))
	}
	for _, out := range outcomes {
		if !out.Censored || out.Fault != "" {
			t.Fatalf("outcome = %+v, want censored with no fault", out)
		}
	}
	sample := Summarise(stage, outcomes)
	if sample.Censored != 3 || sample.Succeeded != 0 || sample.Faults != nil {
		t.Fatalf("sample = %+v", sample)
	}
	if sample.TTFTP99 < 150*time.Millisecond {
		t.Fatalf("ttft p99 = %s, want at least the time they were cut at", sample.TTFTP99)
	}
}

func TestSummariseTakesTheSuccessRateOverSettledRequests(t *testing.T) {
	start := time.Now()
	outcomes := []Outcome{
		{Start: start, TTFT: 10 * time.Millisecond, Total: time.Second, Tokens: 10, Gaps: []time.Duration{time.Millisecond}},
		{Start: start, TTFT: 20 * time.Millisecond, Total: time.Second, Tokens: 10},
		{Start: start, Total: 100 * time.Millisecond, Fault: "http_503", Status: 503},
		{Start: start, Total: 2 * time.Second, Censored: true},
	}
	sample := Summarise(Stage{Concurrency: 4, Duration: time.Second}, outcomes)

	if sample.Requests != 4 || sample.Succeeded != 2 || sample.Censored != 1 || sample.Faults["http_503"] != 1 {
		t.Fatalf("sample = %+v", sample)
	}
	if want := 2.0 / 3.0; sample.SuccessRate != want {
		t.Fatalf("success rate = %v, want %v", sample.SuccessRate, want)
	}
	if sample.TTFTMax != 2*time.Second {
		t.Fatalf("ttft max = %s, want the censored request's cut time", sample.TTFTMax)
	}
	if sample.Elapsed != 2*time.Second {
		t.Fatalf("elapsed = %s, want 2s", sample.Elapsed)
	}
	if sample.Throughput != 1 || sample.TokenRate != 10 {
		t.Fatalf("throughput %v tokens/s %v, want 1 and 10", sample.Throughput, sample.TokenRate)
	}
}

func TestSummariseOfNothing(t *testing.T) {
	sample := Summarise(Stage{Concurrency: 1, Duration: time.Second}, nil)
	if sample.Requests != 0 || sample.SuccessRate != 0 {
		t.Fatalf("sample = %+v", sample)
	}
}

func TestReadStreamCountsOnlyFramesWithContent(t *testing.T) {
	stream := strings.Join([]string{
		`data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}`,
		``,
		`data: {"transit_write_ns":1,"choices":[{"index":0,"delta":{"content":"he"}}]}`,
		``,
		`data: {"choices":[{"index":0,"delta":{}}]}`,
		``,
		`data: {"choices":[{"index":0,"delta":{"reasoning_content":"hmm"}}]}`,
		``,
		`data:{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0}]}}]}`,
		``,
		`data: {"choices":[{"index":0,"delta":{"tool_calls":null}}]}`,
		``,
		`data: {"choices":[],"usage":{"completion_tokens":3}}`,
		``,
		`data: [DONE]`,
		``,
	}, "\n")
	var out Outcome
	readStream(strings.NewReader(stream), time.Now(), &out)

	if out.Tokens != 3 {
		t.Fatalf("tokens = %d, want 3: the role chunk, the empty delta and the usage chunk are not tokens", out.Tokens)
	}
	if len(out.Gaps) != 2 {
		t.Fatalf("gaps = %d, want 2", len(out.Gaps))
	}
	if out.TTFT <= 0 || out.Return <= 0 {
		t.Fatalf("ttft %s return %s, want both from the first content frame", out.TTFT, out.Return)
	}
	if out.Fault != "" {
		t.Fatalf("fault = %q", out.Fault)
	}
}

func TestReadStreamCountsLegacyCompletionText(t *testing.T) {
	var out Outcome
	readStream(strings.NewReader("data: {\"choices\":[{\"text\":\"a\"}]}\n\ndata: {\"choices\":[{\"text\":\"\"}]}\n\n"), time.Now(), &out)
	if out.Tokens != 1 {
		t.Fatalf("tokens = %d, want 1", out.Tokens)
	}
}

func TestOneAgainstTheFakeFrontend(t *testing.T) {
	url := frontend(t, upstream.Profile{TTFT: 5 * time.Millisecond, ITL: time.Millisecond, Tokens: 5})
	for _, stream := range []bool{true, false} {
		out := one(context.Background(), NewClient(1), Request{URL: url, Model: "m", Stream: stream})
		if out.Fault != "" || out.Status != 200 {
			t.Fatalf("stream=%v: outcome %+v", stream, out)
		}
		if stream && (out.Tokens != 5 || len(out.Gaps) != 4 || out.Inbound <= 0) {
			t.Fatalf("stream: tokens %d gaps %d inbound %s", out.Tokens, len(out.Gaps), out.Inbound)
		}
		if !stream && out.Tokens != 1 {
			t.Fatalf("non-stream: tokens %d", out.Tokens)
		}
	}
}

func TestOneBucketsAnHTTPError(t *testing.T) {
	srv := httptest.NewServer(nil) // DefaultServeMux has nothing on the path
	defer srv.Close()
	out := one(context.Background(), NewClient(1), Request{URL: srv.URL + "/v1/chat/completions", Model: "m"})
	if out.Fault != "http_404" || out.Status != 404 {
		t.Fatalf("outcome = %+v", out)
	}
}

func TestParseStages(t *testing.T) {
	stages, err := ParseStages("4, 16,64x30s", 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if len(stages) != 3 || stages[1].Concurrency != 16 || stages[2].Duration != 30*time.Second {
		t.Fatalf("stages = %+v", stages)
	}
	stages, err = ParseStages("8", 5*time.Second)
	if err != nil || stages[0].Duration != 5*time.Second {
		t.Fatalf("stages %+v err %v", stages, err)
	}
	for _, bad := range []string{"", "0", "a,b", "4x", "4xforever"} {
		if _, err := ParseStages(bad, time.Second); err == nil {
			t.Errorf("ParseStages(%q) should fail", bad)
		}
	}
}

func TestFaultBuckets(t *testing.T) {
	cases := map[string]string{
		"context deadline exceeded": "timeout",
		"context canceled":          "canceled",
		"read: connection reset":    "reset",
		"unexpected EOF":            "eof",
		"dial: connection refused":  "refused",
		"something else":            "other",
	}
	for text, want := range cases {
		if got := fault(errString(text)); got != want {
			t.Errorf("fault(%q) = %q, want %q", text, got, want)
		}
	}
	if fault(nil) != "" {
		t.Error("fault(nil) should be empty")
	}
}

type errString string

func (e errString) Error() string { return string(e) }

// The gateway forwards only an allowlist of response headers, so the inbound
// leg has to be readable from the body alone.
func TestTheInboundLegSurvivesAProxyThatDropsTheHeader(t *testing.T) {
	front := httptest.NewServer(upstream.New(upstream.Profile{TTFT: 5 * time.Millisecond, ITL: time.Millisecond, Tokens: 3}).Handler())
	defer front.Close()
	target, _ := neturl.Parse(front.URL)
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.ModifyResponse = func(resp *http.Response) error {
		resp.Header.Del(upstream.HeaderReceived)
		return nil
	}
	srv := httptest.NewServer(proxy)
	defer srv.Close()

	for _, stream := range []bool{true, false} {
		out := one(context.Background(), NewClient(1), Request{URL: srv.URL + "/v1/chat/completions", Model: "m", Stream: stream})
		if out.Fault != "" {
			t.Fatalf("stream=%v: %+v", stream, out)
		}
		if out.Inbound <= 0 || out.Inbound > out.TTFT {
			t.Fatalf("stream=%v: inbound %s ttft %s, want 0 < inbound <= ttft", stream, out.Inbound, out.TTFT)
		}
	}
}
