package serving

import (
	"context"
	"crypto/subtle"
	"errors"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
)

// observation is what one request turns out to have been.
//
// It is assembled as the request travels (the guard knows it was rejected, the
// router knows which endpoint it named and which cluster it went to, the proxy's
// error handler knows the upstream never answered) and read once at the end.
// It is reached through the request context, not passed down, because the
// proxy handlers are pooled per upstream and shared across every request to it:
// per-request state on one of those would be per-request state on a shared
// object.
//
// Nothing on here is, or may become, a label of unbounded cardinality. There is
// no caller, no key, no client address and no free-text model string in this
// struct, and none may be added: identity in this hop is for enforcement and for
// nothing else.
type observation struct {
	start    time.Time
	origin   metrics.Origin
	recorder *metrics.Recorder

	mu           sync.Mutex
	endpoint     string
	endpointName string
	deployment   string
	cluster      string
	outcome      metrics.Outcome
	outcomeSet   bool
	proxied      bool
	firstByte    time.Time
	usage        usageCounts
	haveUsage    bool
	// media is set for an image or video generation request and nil otherwise (media.go).
	media *mediaFacts
}

type observationKey struct{}

// observationFrom returns the observation carried by a request, if any.
func observationFrom(ctx context.Context) *observation {
	obs, _ := ctx.Value(observationKey{}).(*observation)
	return obs
}

// MarkOutcome records what happened to a request, overriding whatever its
// status code would otherwise have implied.
//
// This is the seam the standard's 429 carve-out needs. A 429 issued because an
// identified caller is over its own declared limit is metrics.OutcomeRateLimited
// and is excused; a 429 issued because this hop is shedding, or because an
// upstream refused, is metrics.OutcomeShed and is a failure. They are the same
// status code and only the code that issued one knows which it was, so it says
// so here instead of leaving a query to guess from the number.
func MarkOutcome(r *http.Request, outcome metrics.Outcome) {
	obs := observationFrom(r.Context())
	if obs == nil {
		return
	}
	obs.mu.Lock()
	defer obs.mu.Unlock()
	obs.outcome = outcome
	obs.outcomeSet = true
}

// markEndpoint records which endpoint a request named.
func markEndpoint(r *http.Request, id, name string) {
	obs := observationFrom(r.Context())
	if obs == nil {
		return
	}
	obs.mu.Lock()
	defer obs.mu.Unlock()
	obs.endpoint = id
	obs.endpointName = name
}

// markUpstream records where a request was sent.
func markUpstream(r *http.Request, deployment, cluster string) {
	obs := observationFrom(r.Context())
	if obs == nil {
		return
	}
	obs.mu.Lock()
	defer obs.mu.Unlock()
	obs.deployment = deployment
	obs.cluster = cluster
	obs.proxied = true
}

// markProxyError classifies a request the proxy could not complete.
func markProxyError(r *http.Request, err error) {
	switch {
	case errors.Is(err, context.Canceled):
		MarkOutcome(r, metrics.OutcomeCanceled)
	case isTimeout(err):
		MarkOutcome(r, metrics.OutcomeUpstreamTimeout)
	default:
		MarkOutcome(r, metrics.OutcomeUpstreamError)
	}
}

// markAborted classifies a request that died after its headers had gone out.
//
// This is the only place a mid-stream failure is visible. The response was
// already 200 by then, so without an outcome of its own a truncated generation
// counts as a success, which is the one shape of failure a caller cannot fail to
// notice and the numbers could not see at all.
//
// A caller that hung up and an upstream that died produce the same panic, and the
// request context is what separates them: net/http cancels it when the client
// goes away.
//
// TODO: read the agent's cause trailer instead of folding churn into
// upstream_error. An agent going away mid-stream and a serving runtime going
// away mid-stream arrive here identically, and the additive fix is a trailer the
// agent sets; see the TODOs in the tunnel package's agent.go and classify.go.
// It has to be read off the *http.Response's Trailer map, which means the reading
// happens in the proxy's response path, not in this panic path, and the
// outcome is then handed here through MarkOutcome. Until then an agent rollout
// counts slightly against serving availability: pessimistic but accurate.
func markAborted(r *http.Request, recovered any) {
	err, _ := recovered.(error)
	switch {
	case r.Context().Err() != nil:
		MarkOutcome(r, metrics.OutcomeCanceled)
	case err != nil && errors.Is(err, http.ErrAbortHandler):
		MarkOutcome(r, metrics.OutcomeUpstreamError)
	default:
		// Not the proxy's abort: this hop panicked, and blaming a cluster for it
		// would hide a gateway bug behind cluster blame.
		MarkOutcome(r, metrics.OutcomeInternalError)
	}
}

func isTimeout(err error) bool {
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	var netErr net.Error
	return errors.As(err, &netErr) && netErr.Timeout()
}

func (o *observation) stampFirstByte(now time.Time) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.firstByte.IsZero() {
		o.firstByte = now
	}
}

func (o *observation) recordUsage(counts usageCounts) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.usage = counts
	o.haveUsage = true
}

// sample freezes the observation into what the recorder stores.
func (o *observation) sample(status int, elapsed time.Duration) metrics.Sample {
	o.mu.Lock()
	defer o.mu.Unlock()

	outcome := o.outcome
	if !o.outcomeSet {
		outcome = metrics.OutcomeFromStatus(status)
	}

	sample := metrics.Sample{
		Endpoint:     o.endpoint,
		EndpointName: o.endpointName,
		Origin:       o.origin,
		Deployment:   o.deployment,
		Cluster:      o.cluster,
		Status:       status,
		Outcome:      outcome,
		Duration:     elapsed,
	}

	// Time to first byte is only a measurement of a cluster when a cluster
	// produced the bytes. An error envelope this hop wrote itself has a first
	// byte too, and observing it would report our own 404 as a fast model.
	if o.proxied && !o.firstByte.IsZero() {
		sample.TTFT = o.firstByte.Sub(o.start)
		sample.HaveTTFT = true
	}

	if o.haveUsage {
		sample.HaveUsage = true
		sample.InputTokens = o.usage.input
		sample.OutputTokens = o.usage.output
		sample.CachedInputTokens = o.usage.cachedInput
	}

	if m := o.media; m != nil && m.haveOutputs {
		sample.HaveMedia = true
		sample.MediaKind = m.kind
		sample.MediaOutputs = m.outputs
		sample.UpstreamSeconds = m.upstream.Seconds()
		if m.haveSeconds {
			sample.VideoSeconds = m.seconds
		}
		if m.haveFrames {
			sample.VideoFrames = m.frames
		}
	}
	return sample
}

// mediaFields freezes the media facts for the access line. The facts are only complete once the
// response has finished, which is when the line is written.
func (o *observation) mediaFields() []any {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.media.fields()
}

// ProbeMarker recognises the conformance suite's own traffic.
//
// Two things are required, a header and a token, and the token is why this type
// exists at all: a header anyone could set is a way for any caller to remove
// itself from a denominator we quote to a buyer. It answers one boolean and it
// is not caller identity: it never reaches a label, and a request it does not
// recognise is simply live.
type ProbeMarker struct {
	tokens []string
}

// NewProbeMarker accepts a comma-separated pair, so both values are honoured
// while a rotation is in flight. An empty configuration returns nil, and a nil
// marker calls everything live, which is the correct behaviour for a gateway
// that has no conformance runner pointed at it.
func NewProbeMarker(tokens string) *ProbeMarker {
	marker := &ProbeMarker{}
	for _, token := range strings.Split(tokens, ",") {
		if trimmed := strings.TrimSpace(token); trimmed != "" {
			marker.tokens = append(marker.tokens, trimmed)
		}
	}
	if len(marker.tokens) == 0 {
		return nil
	}
	return marker
}

// Origin classifies one request. Without the token the header is ignored.
func (p *ProbeMarker) Origin(r *http.Request) metrics.Origin {
	if p == nil || !strings.EqualFold(r.Header.Get("X-Hangar-Origin"), string(metrics.OriginProbe)) {
		return metrics.OriginLive
	}
	presented := []byte(r.Header.Get("X-Hangar-Probe-Token"))
	for _, token := range p.tokens {
		if subtle.ConstantTimeCompare(presented, []byte(token)) == 1 {
			return metrics.OriginProbe
		}
	}
	return metrics.OriginLive
}
