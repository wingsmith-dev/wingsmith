package metrics

import "strconv"

// Outcome is what happened to one request, from a closed set.
//
// The set is closed. An operator who can add a member, or redefine
// one, produces an availability number that is not comparable between two
// deployments and is therefore unfalsifiable. What an operator declares is
// which of these sit in the numerator and the denominator of a stated
// objective; what this hop does is observe which one occurred.
//
// Two distinctions in here carry the whole weight of the failure rate:
//
// RateLimited versus Shed. Both are a 429. The acceptance standard excuses a
// 429 issued because an identified caller is over its own declared limit, and
// does not excuse a 429 the gateway issued to protect itself. Only the gateway
// knows which of the two it did, so keying a failure rate off status="429"
// turns every capacity incident into a clean bill of health.
//
// UpstreamError swallowing an upstream 401 or 403. A cluster's serving address
// sits behind a credential this process redeems from the mounted file. When
// that credential goes stale the upstream answers 401, and blaming the caller
// for our own expired secret is a wrong answer that looks right until somebody
// checks.
type Outcome string

const (
	// OutcomeOK is a 2xx delivered to the caller.
	OutcomeOK Outcome = "ok"
	// OutcomeClientError is a 4xx attributable to the request itself:
	// malformed, missing, oversized, or naming a model that is not served.
	OutcomeClientError Outcome = "client_error"
	// OutcomeAuthRejected is a caller that presented no credential or a bad one.
	OutcomeAuthRejected Outcome = "auth_rejected"
	// OutcomeRateLimited is a 429 issued because an identified caller is over a
	// declared limit: a tenant over the gateway's per-tenant in-flight cap or
	// request rate (serving.Limiter).
	OutcomeRateLimited Outcome = "rate_limited"
	// OutcomeShed is a 429 or 503 this hop issued to protect itself, or an
	// upstream's own 429. Not attributable to the caller, so it is a failure.
	OutcomeShed Outcome = "shed"
	// OutcomeNoUpstream is an endpoint that exists and has nowhere to route.
	OutcomeNoUpstream Outcome = "no_upstream"
	// OutcomeUpstreamError is a non-2xx that is not the caller's fault, or a
	// proxy that could not reach the cluster. Includes an upstream 401 or 403.
	OutcomeUpstreamError Outcome = "upstream_error"
	// OutcomeUpstreamTimeout is no response header inside the transport's
	// deadline, or a round trip that ran out of time.
	OutcomeUpstreamTimeout Outcome = "upstream_timeout"
	// OutcomeCanceled is the caller going away before the response completed.
	OutcomeCanceled Outcome = "canceled"
	// OutcomeInternalError is this hop's own fault, and the only member that
	// indicts it. Folding it into OutcomeUpstreamError hides gateway bugs
	// behind cluster blame.
	OutcomeInternalError Outcome = "internal_error"
)

// Outcomes is the closed set, in the order it is documented.
var Outcomes = []Outcome{
	OutcomeOK,
	OutcomeClientError,
	OutcomeAuthRejected,
	OutcomeRateLimited,
	OutcomeShed,
	OutcomeNoUpstream,
	OutcomeUpstreamError,
	OutcomeUpstreamTimeout,
	OutcomeCanceled,
	OutcomeInternalError,
}

// OutcomeFromStatus classifies a response that an upstream produced.
//
// It is only ever consulted for a request that reached a cluster. Everything
// the gateway answers itself names its own outcome at the point it answers,
// because the status code alone cannot distinguish a 503 with no capacity from
// a 503 the caller's own request earned.
func OutcomeFromStatus(status int) Outcome {
	switch {
	case status < 400:
		return OutcomeOK
	case status == 429:
		return OutcomeShed
	case status == 401 || status == 403:
		return OutcomeUpstreamError
	case status < 500:
		return OutcomeClientError
	default:
		return OutcomeUpstreamError
	}
}

// Origin separates real traffic from the conformance suite's probes.
//
// The suite sends malformed requests at a live endpoint and
// asserts on the error envelopes that come back. Those traverse this hop, and
// on a low-traffic endpoint one run inside a ten-minute window trivially
// exceeds a 0.1% budget, so the suite would manufacture the breach it exists
// to detect. Probe-ness is a second axis instead of an eleventh outcome,
// because the probe's own outcome is the entire point of running it.
type Origin string

const (
	// OriginLive is a request from a caller. The default for everything.
	OriginLive Origin = "live"
	// OriginProbe is the conformance suite. Excluded from both sides of an
	// availability ratio, never from one side alone.
	OriginProbe Origin = "probe"
)

// Direction is which way tokens moved.
type Direction string

const (
	DirectionInput       Direction = "input"
	DirectionOutput      Direction = "output"
	DirectionCachedInput Direction = "cached_input"
)

// Input-token bands for the by-input TTFT histogram. Frozen: these boundaries
// are quoted in an acceptance criteria version, and moving one silently
// rewrites what a frozen version measured.
const (
	bucket6k   = 6 * 1024
	bucket16k  = 16 * 1024
	bucket32k  = 32 * 1024
	bucket64k  = 64 * 1024
	bucket128k = 128 * 1024
	bucket256k = 256 * 1024
)

// InputBucketUnknown is where a request that never disclosed its prompt length
// lands. A streaming caller gets a usage object only when it asked for one, and
// this hop does not rewrite a caller's request to improve its own telemetry,
// so the coverage gap goes on the panel, not inside the number.
const InputBucketUnknown = "unknown"

// InputBuckets is the closed set of band labels.
var InputBuckets = []string{
	"lt6k", "6k_16k", "16k_32k", "32k_64k", "64k_128k", "128k_256k", "gt256k", InputBucketUnknown,
}

// InputBucket names the band a prompt length falls in. `known` is false for a
// request whose usage never arrived.
func InputBucket(promptTokens int64, known bool) string {
	if !known || promptTokens < 0 {
		return InputBucketUnknown
	}
	switch {
	case promptTokens < bucket6k:
		return "lt6k"
	case promptTokens < bucket16k:
		return "6k_16k"
	case promptTokens < bucket32k:
		return "16k_32k"
	case promptTokens < bucket64k:
		return "32k_64k"
	case promptTokens < bucket128k:
		return "64k_128k"
	case promptTokens < bucket256k:
		return "128k_256k"
	default:
		return "gt256k"
	}
}

// statusCodes is every code this hop can produce itself, plus the upstream
// codes worth keeping whole.
var statusCodes = map[int]bool{
	200: true, 201: true, 202: true, 204: true,
	400: true, 401: true, 403: true, 404: true, 408: true, 409: true,
	413: true, 422: true, 429: true,
	500: true, 501: true, 502: true, 503: true, 504: true,
}

// StatusLabel renders a status code for the label, from a bounded allowlist.
//
// An upstream can answer with anything, and a status label taken raw is an
// unbounded axis on the busiest series in the process. A code outside the list
// collapses to its class, which is derivable from the code and not the reverse.
func StatusLabel(status int) string {
	if statusCodes[status] {
		return strconv.Itoa(status)
	}
	switch {
	case status >= 500:
		return "500"
	case status >= 400:
		return "400"
	case status >= 300:
		return "300"
	case status >= 200:
		return "200"
	default:
		return "0"
	}
}
