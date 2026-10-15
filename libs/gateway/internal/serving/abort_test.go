package serving

import (
	"net/http"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

// TestATruncatedStreamIsCountedAndStillAborts covers the hole under the
// mid-stream failure. httputil.ReverseProxy reports a body-copy failure by
// panicking http.ErrAbortHandler rather than through ErrorHandler, so before the
// observation was deferred a generation that died halfway through was recorded as
// nothing at all — the 200 its headers carried had already gone out.
//
// The panic has to survive the counting. It is how a handler tells net/http to
// sever the response instead of finishing its framing, and a swallowed one hands
// the caller a truncated stream that looks complete.
func TestATruncatedStreamIsCountedAndStillAborts(t *testing.T) {
	recorder := metrics.New()
	truncating := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("data: {\"choices\":[]}\n\n"))
		panic(http.ErrAbortHandler)
	})
	handler := New(Config{
		Zone: ZoneInternal,
		Router: stubResolver{routes: map[string]routing.Route{
			"chat": handlerUpstream(t, "ep_1", "chat", "dep_1", "ap", truncating),
		}},
		Metrics: recorder,
	})

	aborted := func() (recovered any) {
		defer func() { recovered = recover() }()
		post(t, handler, `{"model":"chat"}`)
		return nil
	}()
	if aborted != http.ErrAbortHandler {
		t.Fatalf("recovered %v, want the abort to be re-raised for net/http", aborted)
	}

	if got := requests(t, recorder, map[string]string{"outcome": "upstream_error", "status": "200"}); got != 1 {
		t.Fatalf("upstream_error at status 200 = %v, want the truncated stream counted once", got)
	}
	if got := requests(t, recorder, map[string]string{"outcome": "ok"}); got != 0 {
		t.Fatalf("ok = %v, want a truncated generation not counted as a success", got)
	}
}

// TestAPanicThatIsNotTheProxysAbortIndictsThisHop. Folding it into
// upstream_error would hide a gateway bug behind cluster blame.
func TestAPanicThatIsNotTheProxysAbortIndictsThisHop(t *testing.T) {
	recorder := metrics.New()
	buggy := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		panic("nil map write, somewhere in here")
	})
	handler := New(Config{
		Zone: ZoneInternal,
		Router: stubResolver{routes: map[string]routing.Route{
			"chat": handlerUpstream(t, "ep_1", "chat", "dep_1", "ap", buggy),
		}},
		Metrics: recorder,
	})

	func() {
		defer func() {
			if recover() == nil {
				t.Error("the panic must keep travelling")
			}
		}()
		post(t, handler, `{"model":"chat"}`)
	}()

	if got := requests(t, recorder, map[string]string{"outcome": "internal_error"}); got != 1 {
		t.Fatalf("internal_error = %v, want 1", got)
	}
}
