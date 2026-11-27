package serving

import (
	"math"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/metrics"
	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/routing"
)

// mediaUnitsOf are the billable units each media kind spends, as `unit_limits` names them.
var mediaUnitsOf = map[string][]string{
	mediaImage:         {"image_units"},
	mediaVideo:         {"video_seconds"},
	mediaSpeech:        {"speech_characters", "speech_seconds"},
	mediaTranscription: {"transcription_seconds"},
}

// UnitQuotas enforces an endpoint's media unit budgets (`unit_limits`, units per minute) on this
// replica, spending them from the same numbers wingsmith_gateway_generated_total counts.
//
// A unit known from the request (a speech request's characters) is charged before the forward
// and refused when the budget cannot cover it. A unit only the response states (images times
// size tier, seconds of audio or video) is charged after it, so the budget may go negative and
// the endpoint is refused until it refills: an image cannot be un-generated, and guessing its
// cost up front would refuse requests the budget can afford.
//
// Each budget is a bucket of `limit` units refilled at `limit` per minute. Under `divided` this
// replica holds `limit / replicas`. Not shared across replicas, for the reason the plane's
// limit_scope gives. A nil *UnitQuotas enforces nothing.
type UnitQuotas struct {
	replicas int
	now      func() time.Time

	mu      sync.Mutex
	buckets map[string]*unitBucket
}

type unitBucket struct {
	limit  float64
	tokens float64
	last   time.Time
}

// NewUnitQuotas builds the enforcer for a fleet of `replicas` gateways (less than one is one).
func NewUnitQuotas(replicas int) *UnitQuotas {
	return &UnitQuotas{replicas: max(replicas, 1), now: time.Now, buckets: map[string]*unitBucket{}}
}

// limitFor is this replica's per-minute budget of a unit on a route, and whether it has one.
func (q *UnitQuotas) limitFor(route routing.Route, unit string) (float64, bool) {
	if route.Limits == nil {
		return 0, false
	}
	limit, ok := route.Limits.UnitLimits[unit]
	if !ok || limit <= 0 {
		return 0, false
	}
	if route.Limits.LimitScope == "divided" {
		return math.Max(1, math.Floor(float64(limit)/float64(q.replicas))), true
	}
	return float64(limit), true
}

// bucket returns the refilled bucket for a route's unit. The caller holds q.mu.
func (q *UnitQuotas) bucket(route routing.Route, unit string, limit float64) *unitBucket {
	key := route.ID + "/" + unit
	now := q.now()
	b := q.buckets[key]
	if b == nil || b.limit != limit {
		b = &unitBucket{limit: limit, tokens: limit, last: now}
		q.buckets[key] = b
		return b
	}
	b.tokens = math.Min(b.limit, b.tokens+now.Sub(b.last).Seconds()*b.limit/60)
	b.last = now
	return b
}

// admit charges what a request is known to cost and reports whether every budget its kind spends
// can take it. On a refusal nothing is charged, and retryAfter says when the shortest wait ends.
func (q *UnitQuotas) admit(route routing.Route, kind string, upfront map[string]float64) (retryAfter time.Duration, ok bool) {
	if q == nil {
		return 0, true
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	enforced := route.Limits != nil && route.Limits.Enforced
	type charge struct {
		b    *unitBucket
		cost float64
	}
	var charges []charge
	for _, unit := range mediaUnitsOf[kind] {
		limit, has := q.limitFor(route, unit)
		if !has {
			continue
		}
		b := q.bucket(route, unit, limit)
		cost := upfront[unit]
		if enforced && (b.tokens <= 0 || cost > b.tokens) {
			need := math.Max(cost-b.tokens, -b.tokens) + math.SmallestNonzeroFloat64
			wait := time.Duration(need / (b.limit / 60) * float64(time.Second))
			return max(wait, time.Second), false
		}
		charges = append(charges, charge{b, cost})
	}
	for _, c := range charges {
		c.b.tokens -= c.cost
	}
	return 0, true
}

// spend charges what a finished request cost beyond what admit already charged.
func (q *UnitQuotas) spend(route routing.Route, kind string, units, upfront map[string]float64) {
	if q == nil {
		return
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	for _, unit := range mediaUnitsOf[kind] {
		limit, has := q.limitFor(route, unit)
		if !has {
			continue
		}
		if extra := units[unit] - upfront[unit]; extra != 0 {
			q.bucket(route, unit, limit).tokens -= extra
		}
	}
}

// stageUnitQuota admits a media request against its endpoint's unit budgets and spends what it
// produced once it is finished. After stageMediaFacts, which is what knows the kind and what the
// request asked for.
func stageUnitQuota(quotas *UnitQuotas) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if quotas == nil || req.route.Limits == nil || len(req.route.Limits.UnitLimits) == 0 {
			return r, true
		}
		obs := observationFrom(r.Context())
		if obs == nil {
			return r, true
		}
		obs.mu.Lock()
		facts := obs.media
		var kind string
		upfront := map[string]float64{}
		if facts != nil {
			kind = facts.kind
			if kind == mediaSpeech {
				upfront["speech_characters"] = float64(facts.characters)
			}
		}
		obs.mu.Unlock()
		if kind == "" {
			return r, true
		}
		route := req.route
		// A job reserves what it asked for at submit and keeps it; the worker that runs it does
		// not admit again and settles only the difference (docs/decisions/0041).
		run := jobRunFrom(r.Context())
		switch {
		case run != nil:
			upfront = run.reserved
		case req.asyncKind != "":
			for unit, v := range reservedUnits(kind, req.body) {
				upfront[unit] = v
			}
		}
		if run == nil {
			retryAfter, ok := quotas.admit(req.route, kind, upfront)
			if !ok {
				w.Header().Set("Retry-After", strconv.Itoa(int(math.Ceil(retryAfter.Seconds()))))
				MarkOutcome(r, metrics.OutcomeRateLimited)
				writeError(w, http.StatusTooManyRequests, "rate_limited",
					"The model '"+req.route.Name+"' is over its media quota. Retry shortly.")
				return r, false
			}
		}
		req.release = append(req.release, func() {
			if req.asyncSubmitted {
				return
			}
			obs.mu.Lock()
			units := obs.media.units()
			obs.mu.Unlock()
			quotas.spend(route, kind, units, upfront)
		})
		return r, true
	}
}
