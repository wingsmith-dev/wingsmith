package serving

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

func TestALimiterThatBoundsNothingIsNil(t *testing.T) {
	if NewLimiter(TenantLimits{}) != nil {
		t.Fatal("zero limits should build no limiter")
	}
	var l *Limiter
	release, _, ok := l.admit("ten_a")
	if !ok {
		t.Fatal("a nil limiter must admit everything")
	}
	release()
}

func TestTheInFlightCapIsPerTenantAndReleased(t *testing.T) {
	l := NewLimiter(TenantLimits{MaxInFlight: 2})
	first, _, ok1 := l.admit("ten_a")
	_, _, ok2 := l.admit("ten_a")
	_, _, ok3 := l.admit("ten_a")
	if !ok1 || !ok2 || ok3 {
		t.Fatalf("admitted %v %v %v, want the third refused", ok1, ok2, ok3)
	}
	if _, _, ok := l.admit("ten_b"); !ok {
		t.Fatal("another tenant's cap was spent by ten_a")
	}
	first()
	first() // a double release must not free a second slot
	if _, _, ok := l.admit("ten_a"); !ok {
		t.Fatal("a released slot was not returned")
	}
	if _, _, ok := l.admit("ten_a"); ok {
		t.Fatal("a double release freed two slots")
	}
}

func TestTheRateLimitRefillsOverTime(t *testing.T) {
	now := time.Unix(0, 0)
	l := NewLimiter(TenantLimits{RPS: 2, Burst: 2})
	l.now = func() time.Time { return now }

	for i := 0; i < 2; i++ {
		release, _, ok := l.admit("ten_a")
		if !ok {
			t.Fatalf("request %d refused inside the burst", i)
		}
		release()
	}
	_, wait, ok := l.admit("ten_a")
	if ok || wait <= 0 {
		t.Fatalf("ok = %v, wait = %v, want refused with a wait", ok, wait)
	}
	now = now.Add(500 * time.Millisecond)
	if _, _, ok := l.admit("ten_a"); !ok {
		t.Fatal("half a second at 2 rps should have refilled one request")
	}
}

// TestAnInternalTenantOverItsCapGetsRateLimited goes through the handler: the
// tenant on the internal zone is the endpoint's owner, the answer is the
// existing 429 rate_limited, and it is counted as the caller's limit rather
// than as this hop shedding.
func TestAnInternalTenantOverItsCapGetsRateLimited(t *testing.T) {
	release := make(chan struct{})
	entered := make(chan struct{}, 1)
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		entered <- struct{}{}
		<-release
		w.WriteHeader(http.StatusOK)
	})
	route := handlerUpstream(t, "ep_chat", "chat", "dep_1", "ap", upstream)
	route.TenantID = "ten_globex"

	recorder := metrics.New()
	handler := New(Config{
		Zone:    ZoneInternal,
		Router:  stubResolver{routes: map[string]routing.Route{"chat": route}},
		Tenants: tenantsFor(t, globex()),
		Limiter: NewLimiter(TenantLimits{MaxInFlight: 1}),
		Metrics: recorder,
	})

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`)))
	}()
	<-entered

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`)))
	close(release)
	wg.Wait()

	if rec.Code != http.StatusTooManyRequests || !strings.Contains(rec.Body.String(), "rate_limited") {
		t.Fatalf("status = %d, body = %s, want 429 rate_limited", rec.Code, rec.Body)
	}
	if rec.Header().Get("Retry-After") == "" {
		t.Fatal("a 429 without Retry-After")
	}
	if got := requests(t, recorder, map[string]string{"outcome": string(metrics.OutcomeRateLimited)}); got != 1 {
		t.Fatalf("rate_limited outcomes = %v, want 1", got)
	}

	// Released: the next request is admitted again.
	go func() { <-entered }()
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`)))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d after release, want 200", rec.Code)
	}
}

// TestAPublicTenantIsLimitedBeforeItsBodyIsRead pins where the public limit
// sits: the host names the tenant, so a tenant over its rate is refused before
// this hop buffers anything it sent.
func TestAPublicTenantIsLimitedBeforeItsBodyIsRead(t *testing.T) {
	server, _ := upstreamServer(t)
	handler := New(Config{
		Zone:    ZonePublic,
		Router:  stubResolver{routes: map[string]routing.Route{"chat": globexRoute(t, server.URL)}},
		Tenants: tenantsFor(t, globex()),
		Limiter: NewLimiter(TenantLimits{RPS: 0.001, Burst: 1}),
	})
	send := func(body *countingReader) int {
		r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", body)
		r.Host = "globex.hangar.dev"
		r.Header.Set("Authorization", "Bearer secret-key")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, r)
		return rec.Code
	}
	if code := send(&countingReader{body: strings.NewReader(`{"model":"chat"}`)}); code != http.StatusOK {
		t.Fatalf("first status = %d", code)
	}
	second := &countingReader{body: strings.NewReader(`{"model":"chat"}`)}
	if code := send(second); code != http.StatusTooManyRequests {
		t.Fatalf("second status = %d, want 429", code)
	}
	if second.read != 0 {
		t.Fatalf("read %d bytes of a refused request", second.read)
	}
}

type countingReader struct {
	body *strings.Reader
	read int
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.body.Read(p)
	c.read += n
	return n, err
}
