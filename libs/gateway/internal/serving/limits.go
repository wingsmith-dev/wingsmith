package serving

import (
	"math"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/metrics"
)

// DefaultTenantMaxInFlight is how many requests one tenant may have in flight
// on one replica when nothing says otherwise. High enough that an operator's
// own edge, which is usually one tenant carrying every customer, does not meet
// it in normal use; low enough that one tenant cannot hold every connection
// this replica has.
const DefaultTenantMaxInFlight = 1024

// TenantLimits bounds what one tenant may ask of one gateway replica.
//
// These are protective limits, set per replica by the operator. They are not
// the serving quota an endpoint sells, which the plane publishes and this hop
// does not enforce yet.
type TenantLimits struct {
	// MaxInFlight is the most requests one tenant may have in flight at once.
	// Zero or less is unlimited.
	MaxInFlight int
	// RPS is the sustained request rate one tenant may send. Zero or less is
	// no rate limit.
	RPS float64
	// Burst is how many requests above RPS a tenant may send at once. Less
	// than one takes the ceiling of RPS, and at least one.
	Burst int
}

// Limiter enforces TenantLimits, keyed by tenant id. One Limiter is shared by
// every listener on a replica, so a tenant reaching both does not get twice the
// allowance. A nil *Limiter enforces nothing.
type Limiter struct {
	limits TenantLimits
	burst  float64
	now    func() time.Time

	mu      sync.Mutex
	tenants map[string]*tenantUsage
}

type tenantUsage struct {
	inFlight int
	tokens   float64
	last     time.Time
}

// NewLimiter builds a limiter, or nil when the limits bound nothing.
func NewLimiter(limits TenantLimits) *Limiter {
	if limits.MaxInFlight <= 0 && limits.RPS <= 0 {
		return nil
	}
	burst := float64(limits.Burst)
	if burst < 1 {
		burst = math.Max(1, math.Ceil(limits.RPS))
	}
	return &Limiter{limits: limits, burst: burst, now: time.Now, tenants: map[string]*tenantUsage{}}
}

// admit takes one request for a tenant. It returns the release to call when
// the request is finished, or ok=false and how long to wait before retrying.
func (l *Limiter) admit(tenant string) (release func(), retryAfter time.Duration, ok bool) {
	if l == nil || tenant == "" {
		return func() {}, 0, true
	}
	l.mu.Lock()
	defer l.mu.Unlock()

	usage := l.tenants[tenant]
	if usage == nil {
		usage = &tenantUsage{tokens: l.burst, last: l.now()}
		l.tenants[tenant] = usage
	}
	if l.limits.MaxInFlight > 0 && usage.inFlight >= l.limits.MaxInFlight {
		return nil, time.Second, false
	}
	if l.limits.RPS > 0 {
		now := l.now()
		usage.tokens = math.Min(l.burst, usage.tokens+now.Sub(usage.last).Seconds()*l.limits.RPS)
		usage.last = now
		if usage.tokens < 1 {
			wait := time.Duration((1 - usage.tokens) / l.limits.RPS * float64(time.Second))
			return nil, wait, false
		}
		usage.tokens--
	}
	usage.inFlight++

	var once sync.Once
	return func() {
		once.Do(func() {
			l.mu.Lock()
			defer l.mu.Unlock()
			usage.inFlight--
			// Forget an idle tenant whose bucket is full again, so the map holds
			// the tenants that are busy instead of every tenant ever seen.
			if usage.inFlight == 0 && (l.limits.RPS <= 0 || usage.tokens+l.now().Sub(usage.last).Seconds()*l.limits.RPS >= l.burst) {
				delete(l.tenants, tenant)
			}
		})
	}, 0, true
}

// stageTenantLimit admits a request against its tenant's limits, and holds the
// admission until the request is finished.
//
// On the public zone the tenant is the host's and is known before the body is
// read, which is where a limit does the most good. On the internal zone the
// tenant is the endpoint's owner, so the stage runs again after stageResolve;
// each call only acts on the zone it is placed for.
//
// The answer is the same 429 rate_limited an engine's own limit gets, counted
// as OutcomeRateLimited: the caller is over a limit it can see, which is not a
// failure of this hop.
func stageTenantLimit(limiter *Limiter, zone Zone) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if limiter == nil || req.zone != zone {
			return r, true
		}
		tenant := req.tenant.ID
		if zone == ZoneInternal {
			tenant = req.route.TenantID
		}
		release, retryAfter, ok := limiter.admit(tenant)
		if !ok {
			seconds := int(math.Ceil(retryAfter.Seconds()))
			if seconds < 1 {
				seconds = 1
			}
			w.Header().Set("Retry-After", strconv.Itoa(seconds))
			MarkOutcome(r, metrics.OutcomeRateLimited)
			writeError(w, http.StatusTooManyRequests, "rate_limited",
				"Too many requests for this tenant. Retry shortly.")
			return r, false
		}
		req.release = append(req.release, release)
		return r, true
	}
}
