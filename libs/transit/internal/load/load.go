// Package load drives concurrent OpenAI-compatible completions and reports what
// the caller experienced.
//
// It measures the three things a streaming caller can actually feel, when the
// first token arrived, how evenly the rest arrived, and whether the request
// finished at all, and, where the far end stamps its own clock, it splits the
// first of those into the leg that went in and the leg that came back. Nothing
// here knows what it is pointed at: the same generator runs against a fake
// frontend on a loopback port and against a deployed gateway, which is what
// makes a laptop number and a production number the same shape.
package load

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/hangar-dev/hangar/libs/transit/internal/upstream"
)

// Request is one completion, repeated.
type Request struct {
	// URL is the completions endpoint, including the path.
	URL string
	// Model is the public name to ask for.
	Model string
	// Token is the bearer presented, empty for an open gateway.
	Token string
	// Stream asks for SSE. False measures the non-streaming path, where the
	// whole answer arrives at once and TTFT is the whole answer.
	Stream bool
	// PromptBytes pads the body, so a run can measure what a long prompt costs
	// the hop that has to buffer it to read `model`.
	PromptBytes int
	// Schedule, when the far end is the fake frontend, asks it for a particular
	// TTFT, inter-token gap and length. A real frontend ignores it.
	Schedule upstream.Profile
	// Headers are stamped on every request.
	Headers map[string]string
}

// Outcome is what happened to one request.
type Outcome struct {
	Start time.Time
	// TTFT is send to first content byte at the caller.
	TTFT time.Duration
	// Total is send to the end of the body.
	Total time.Duration
	// Inbound is send to the far end receiving it, where the far end says.
	Inbound time.Duration
	// Return is the far end writing the first token to the caller seeing it.
	Return time.Duration
	// Gaps are the inter-token arrivals after the first.
	Gaps []time.Duration
	// Tokens is how many content chunks arrived.
	Tokens int
	// Bytes is the response body size.
	Bytes int64
	// Status is the HTTP status, zero when the request never got one.
	Status int
	// Fault buckets a failure for the report, empty on success.
	Fault string
	// Censored marks a request the harness cut off at the end of a stage's
	// drain, which says nothing about whether the system would have served it.
	// It is neither a success nor a failure; Total is how long it had run, and
	// TTFT is set only if its first token arrived.
	Censored bool
}

// Stage is one step of a ramp.
type Stage struct {
	Concurrency int
	// Duration is how long workers keep starting requests.
	Duration time.Duration
	// Drain is how long requests still in flight when Duration ends are given
	// to finish before they are cut off and recorded as censored. Zero takes
	// DefaultDrain.
	Drain time.Duration
}

// DefaultDrain is the drain a stage gets when it names none.
const DefaultDrain = 30 * time.Second

// NewClient builds the generator's HTTP client.
//
// No timeout of any kind. A generation legitimately runs for minutes, and a
// client-side deadline would record the harness's impatience as the system's
// failure, which is the single easiest way to publish a wrong capacity number.
func NewClient(concurrency int) *http.Client {
	if concurrency < 16 {
		concurrency = 16
	}
	return &http.Client{
		Transport: &http.Transport{
			MaxIdleConns:        concurrency * 2,
			MaxIdleConnsPerHost: concurrency * 2,
			MaxConnsPerHost:     0,
			IdleConnTimeout:     90 * time.Second,
			DisableCompression:  true,
		},
	}
}

// Run drives one stage to completion and returns every outcome in it.
//
// Closed-loop: each worker holds one request open at a time, so concurrency is
// the number of requests in flight and never a rate. That is the right model for
// this system, a caller of an inference API waits for its answer, and it is
// also the only one under which a saturated hop shows up as latency, not
// as an unbounded queue in the generator.
//
// When the stage's duration ends, workers stop starting requests and the ones
// in flight are left to finish for up to the stage's drain. Cancelling them at
// the boundary instead would drop exactly the slowest requests of every stage,
// and a percentile computed over the survivors is optimistic by construction.
// Whatever is still running when the drain ends is cut off and returned with
// Censored set, so the report can say how many there were.
func Run(ctx context.Context, client *http.Client, req Request, stage Stage) []Outcome {
	hold, stopStarting := context.WithTimeout(ctx, stage.Duration)
	defer stopStarting()
	inflight, cutOff := context.WithCancel(ctx)
	defer cutOff()

	drain := stage.Drain
	if drain <= 0 {
		drain = DefaultDrain
	}
	finished := make(chan struct{})
	go func() {
		select {
		case <-finished:
			return
		case <-hold.Done():
		}
		timer := time.NewTimer(drain)
		defer timer.Stop()
		select {
		case <-finished:
		case <-timer.C:
			cutOff()
		}
	}()

	var mu sync.Mutex
	outcomes := make([]Outcome, 0, 1024)
	var live atomic.Int64
	var peak atomic.Int64

	var wg sync.WaitGroup
	for i := 0; i < stage.Concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for hold.Err() == nil {
				n := live.Add(1)
				for {
					was := peak.Load()
					if n <= was || peak.CompareAndSwap(was, n) {
						break
					}
				}
				out := one(inflight, client, req)
				live.Add(-1)
				// A request the harness itself cut off is not a failure of the
				// system under test. Counting it as one would put fabricated
				// errors into every stage; dropping it would hide the slowest
				// requests. It is kept, marked.
				if inflight.Err() != nil && out.Fault != "" {
					out.Fault = ""
					out.Censored = true
				}
				mu.Lock()
				outcomes = append(outcomes, out)
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	close(finished)
	return outcomes
}

// one sends a single completion and times what comes back.
func one(ctx context.Context, client *http.Client, req Request) Outcome {
	body, err := encode(req)
	if err != nil {
		return Outcome{Fault: "encode"}
	}

	start := time.Now()
	out := Outcome{Start: start}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, req.URL, bytes.NewReader(body))
	if err != nil {
		out.Fault = "build"
		return out
	}
	httpReq.Header.Set("Content-Type", "application/json")
	if req.Token != "" {
		httpReq.Header.Set("Authorization", "Bearer "+req.Token)
	}
	for name, value := range req.Headers {
		httpReq.Header.Set(name, value)
	}

	resp, err := client.Do(httpReq)
	if err != nil {
		out.Total = time.Since(start)
		out.Fault = fault(err)
		return out
	}
	defer func() { _ = resp.Body.Close() }()

	out.Status = resp.StatusCode
	if received := resp.Header.Get(upstream.HeaderReceived); received != "" {
		if ns, err := strconv.ParseInt(received, 10, 64); err == nil {
			out.Inbound = time.Unix(0, ns).Sub(start)
		}
	}
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, resp.Body)
		out.Total = time.Since(start)
		out.Fault = "http_" + strconv.Itoa(resp.StatusCode)
		return out
	}

	if req.Stream {
		readStream(resp.Body, start, &out)
	} else {
		var answer bytes.Buffer
		n, err := io.Copy(&answer, io.LimitReader(resp.Body, 1<<20))
		if err == nil {
			var extra int64
			extra, err = io.Copy(io.Discard, resp.Body)
			n += extra
		}
		out.Bytes = n
		out.TTFT = time.Since(start)
		out.Tokens = 1
		if err != nil {
			out.Fault = fault(err)
		}
		var stamped chunk
		if out.Inbound == 0 && json.Unmarshal(answer.Bytes(), &stamped) == nil && stamped.ReceivedNs > 0 {
			out.Inbound = time.Unix(0, stamped.ReceivedNs).Sub(start)
		}
	}
	out.Total = time.Since(start)
	if out.Fault == "" && out.Tokens == 0 {
		out.Fault = "empty"
	}
	return out
}

// chunk is the part of an SSE frame this generator reads: the far end's own
// write timestamp, when it sets one, and whatever content the choices carry.
type chunk struct {
	ReceivedNs int64 `json:"transit_received_ns"`
	WriteNs    int64 `json:"transit_write_ns"`
	Choices    []struct {
		Text  string `json:"text"`
		Delta struct {
			Content          string          `json:"content"`
			ReasoningContent string          `json:"reasoning_content"`
			Reasoning        string          `json:"reasoning"`
			ToolCalls        json.RawMessage `json:"tool_calls"`
		} `json:"delta"`
	} `json:"choices"`
}

// content reports whether a frame carries anything generated. A frame that
// only names the role, an empty delta, or the usage chunk is not a token: an
// engine sends the role chunk before prefill finishes, and timing TTFT from it
// would measure the engine's eagerness instead of its first token.
func (c chunk) content() bool {
	for _, choice := range c.Choices {
		d := choice.Delta
		if choice.Text != "" || d.Content != "" || d.ReasoningContent != "" || d.Reasoning != "" {
			return true
		}
		if len(d.ToolCalls) > 0 && !bytes.Equal(d.ToolCalls, []byte("null")) && !bytes.Equal(d.ToolCalls, []byte("[]")) {
			return true
		}
	}
	return false
}

// readStream times each SSE frame that carries content as it arrives.
//
// A frame carrying no content, the role chunk, the usage chunk, the
// terminator, is not a token and must not enter the inter-token distribution,
// or every stream would show a fictitious final gap of zero.
func readStream(body io.Reader, start time.Time, out *Outcome) {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 64<<10), 4<<20)
	previous := time.Time{}

	for scanner.Scan() {
		line := scanner.Bytes()
		out.Bytes += int64(len(line)) + 1
		if !bytes.HasPrefix(line, []byte("data:")) {
			continue
		}
		payload := bytes.TrimSpace(line[len("data:"):])
		if bytes.Equal(payload, []byte("[DONE]")) {
			continue
		}
		var frame chunk
		if err := json.Unmarshal(payload, &frame); err != nil || !frame.content() {
			continue
		}

		now := time.Now()
		out.Tokens++
		if out.Tokens == 1 {
			out.TTFT = now.Sub(start)
			if frame.WriteNs > 0 {
				out.Return = now.Sub(time.Unix(0, frame.WriteNs))
			}
			if out.Inbound == 0 && frame.ReceivedNs > 0 {
				out.Inbound = time.Unix(0, frame.ReceivedNs).Sub(start)
			}
		} else {
			out.Gaps = append(out.Gaps, now.Sub(previous))
		}
		previous = now
	}
	if err := scanner.Err(); err != nil {
		out.Fault = fault(err)
	}
}

func encode(req Request) ([]byte, error) {
	fields := map[string]any{
		"model":  req.Model,
		"stream": req.Stream,
		"messages": []map[string]string{
			{"role": "user", "content": prompt(req.PromptBytes)},
		},
	}
	if req.Stream {
		fields["stream_options"] = map[string]any{"include_usage": true}
	}
	if req.Schedule.TTFT > 0 {
		fields["hangar_ttft_ms"] = int(req.Schedule.TTFT / time.Millisecond)
	}
	if req.Schedule.ITL > 0 {
		fields["hangar_itl_ms"] = int(req.Schedule.ITL / time.Millisecond)
	}
	if req.Schedule.Tokens > 0 {
		fields["hangar_tokens"] = req.Schedule.Tokens
		fields["max_tokens"] = req.Schedule.Tokens
	}
	return json.Marshal(fields)
}

func prompt(n int) string {
	if n <= 0 {
		n = 64
	}
	var b strings.Builder
	b.Grow(n)
	for b.Len() < n {
		b.WriteString("hangar transit probe. ")
	}
	return b.String()[:n]
}

// fault buckets a transport failure into a label the report can count.
//
// The buckets are the ones the tunnel's own classification distinguishes, so a
// harness run and a gateway's metrics can be read side by side, not
// translated.
func fault(err error) string {
	if err == nil {
		return ""
	}
	text := err.Error()
	switch {
	case strings.Contains(text, "context deadline exceeded"):
		return "timeout"
	case strings.Contains(text, "context canceled"):
		return "canceled"
	case strings.Contains(text, "connection reset"):
		return "reset"
	case strings.Contains(text, "EOF"):
		return "eof"
	case strings.Contains(text, "connection refused"):
		return "refused"
	default:
		return "other"
	}
}

// Sample is one stage, summarised.
type Sample struct {
	Concurrency int            `json:"concurrency"`
	Duration    time.Duration  `json:"duration"`
	Requests    int            `json:"requests"`
	Succeeded   int            `json:"succeeded"`
	Faults      map[string]int `json:"faults,omitempty"`
	// Censored is how many requests were still running when the stage's drain
	// ended. They count towards Requests and towards neither Succeeded nor
	// Faults, and the success rate is taken over the rest.
	Censored    int     `json:"censored,omitempty"`
	SuccessRate float64 `json:"success_rate"`
	// Elapsed is first request sent to last request ended, drain included. The
	// rates are per second of it.
	Elapsed time.Duration `json:"elapsed"`

	// The TTFT percentiles include censored requests: the ones whose first
	// token arrived at its real time, and the ones whose first token never did
	// at the time they were cut off, which is a lower bound on their TTFT.
	// Leaving those out would make p99 better the worse the tail was.
	TTFTP50 time.Duration `json:"ttft_p50"`
	TTFTP90 time.Duration `json:"ttft_p90"`
	TTFTP99 time.Duration `json:"ttft_p99"`
	TTFTMax time.Duration `json:"ttft_max"`

	TotalP50 time.Duration `json:"total_p50"`
	TotalP99 time.Duration `json:"total_p99"`

	// InboundP50 and ReturnP50 split TTFT where the far end stamped its clock.
	// Zero means it did not, which is every live run.
	InboundP50 time.Duration `json:"inbound_p50"`
	InboundP99 time.Duration `json:"inbound_p99"`
	ReturnP50  time.Duration `json:"return_p50"`
	ReturnP99  time.Duration `json:"return_p99"`

	// GapP50 and GapP99 are the inter-token arrivals, and GapJitter is their
	// standard deviation. Jitter is the number that exposes head-of-line
	// blocking: a stalled connection does not lengthen a stream evenly, it
	// bunches it.
	GapP50    time.Duration `json:"gap_p50"`
	GapP99    time.Duration `json:"gap_p99"`
	GapMax    time.Duration `json:"gap_max"`
	GapJitter time.Duration `json:"gap_jitter"`

	Throughput float64 `json:"requests_per_second"`
	TokenRate  float64 `json:"tokens_per_second"`
	TokensMin  float64 `json:"tokens_per_minute"`
}

// Summarise reduces one stage's outcomes to a sample.
func Summarise(stage Stage, outcomes []Outcome) Sample {
	sample := Sample{
		Concurrency: stage.Concurrency,
		Duration:    stage.Duration,
		Requests:    len(outcomes),
		Faults:      map[string]int{},
	}
	if len(outcomes) == 0 {
		return sample
	}

	var ttft, total, inbound, ret, gaps []time.Duration
	tokens := 0
	var first, end time.Time
	for _, out := range outcomes {
		if !out.Start.IsZero() {
			if first.IsZero() || out.Start.Before(first) {
				first = out.Start
			}
			if done := out.Start.Add(out.Total); done.After(end) {
				end = done
			}
		}
		if out.Censored {
			sample.Censored++
			tokens += out.Tokens
			gaps = append(gaps, out.Gaps...)
			if out.Tokens > 0 {
				ttft = append(ttft, out.TTFT)
			} else {
				ttft = append(ttft, out.Total)
			}
			continue
		}
		if out.Fault != "" {
			sample.Faults[out.Fault]++
			continue
		}
		sample.Succeeded++
		tokens += out.Tokens
		ttft = append(ttft, out.TTFT)
		total = append(total, out.Total)
		if out.Inbound > 0 {
			inbound = append(inbound, out.Inbound)
		}
		if out.Return > 0 {
			ret = append(ret, out.Return)
		}
		gaps = append(gaps, out.Gaps...)
	}
	if len(sample.Faults) == 0 {
		sample.Faults = nil
	}
	if settled := sample.Requests - sample.Censored; settled > 0 {
		sample.SuccessRate = float64(sample.Succeeded) / float64(settled)
	}

	sort.Slice(ttft, func(i, j int) bool { return ttft[i] < ttft[j] })
	sort.Slice(total, func(i, j int) bool { return total[i] < total[j] })
	sort.Slice(inbound, func(i, j int) bool { return inbound[i] < inbound[j] })
	sort.Slice(ret, func(i, j int) bool { return ret[i] < ret[j] })
	sort.Slice(gaps, func(i, j int) bool { return gaps[i] < gaps[j] })

	sample.TTFTP50, sample.TTFTP90, sample.TTFTP99 = quantile(ttft, 0.50), quantile(ttft, 0.90), quantile(ttft, 0.99)
	sample.TTFTMax = last(ttft)
	sample.TotalP50, sample.TotalP99 = quantile(total, 0.50), quantile(total, 0.99)
	sample.InboundP50, sample.InboundP99 = quantile(inbound, 0.50), quantile(inbound, 0.99)
	sample.ReturnP50, sample.ReturnP99 = quantile(ret, 0.50), quantile(ret, 0.99)
	sample.GapP50, sample.GapP99, sample.GapMax = quantile(gaps, 0.50), quantile(gaps, 0.99), last(gaps)
	sample.GapJitter = stddev(gaps)

	sample.Elapsed = end.Sub(first)
	if sample.Elapsed <= 0 {
		sample.Elapsed = stage.Duration
	}
	if seconds := sample.Elapsed.Seconds(); seconds > 0 {
		sample.Throughput = float64(sample.Succeeded) / seconds
		sample.TokenRate = float64(tokens) / seconds
		sample.TokensMin = sample.TokenRate * 60
	}
	return sample
}

func quantile(sorted []time.Duration, q float64) time.Duration {
	if len(sorted) == 0 {
		return 0
	}
	idx := int(math.Ceil(q*float64(len(sorted)))) - 1
	if idx < 0 {
		idx = 0
	}
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return sorted[idx]
}

func last(sorted []time.Duration) time.Duration {
	if len(sorted) == 0 {
		return 0
	}
	return sorted[len(sorted)-1]
}

func stddev(values []time.Duration) time.Duration {
	if len(values) < 2 {
		return 0
	}
	var sum float64
	for _, v := range values {
		sum += float64(v)
	}
	mean := sum / float64(len(values))
	var variance float64
	for _, v := range values {
		d := float64(v) - mean
		variance += d * d
	}
	return time.Duration(math.Sqrt(variance / float64(len(values)-1)))
}

// ParseStages reads a ramp such as "4,16,64x30s": a list of concurrencies and
// one duration each stage holds for.
func ParseStages(spec string, hold time.Duration) ([]Stage, error) {
	if at := strings.LastIndex(spec, "x"); at >= 0 {
		d, err := time.ParseDuration(spec[at+1:])
		if err != nil {
			return nil, fmt.Errorf("stage duration %q: %w", spec[at+1:], err)
		}
		hold, spec = d, spec[:at]
	}
	var stages []Stage
	for _, field := range strings.Split(spec, ",") {
		field = strings.TrimSpace(field)
		if field == "" {
			continue
		}
		n, err := strconv.Atoi(field)
		if err != nil || n < 1 {
			return nil, fmt.Errorf("concurrency %q is not a positive number", field)
		}
		stages = append(stages, Stage{Concurrency: n, Duration: hold})
	}
	if len(stages) == 0 {
		return nil, fmt.Errorf("no stages in %q", spec)
	}
	return stages, nil
}
