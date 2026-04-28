package tunnel

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

// TestReplayableUsesTheBodyTheCallerAlreadyHolds covers the exported seam.
//
// The gateway has already buffered the body to read its `model`, so it can hand
// that buffer over instead of making the pool read the request a second time.
// This checks the shortcut actually produces a replayable request — and that
// makeReplayable then leaves it alone, which is what makes the shortcut a
// shortcut.
func TestReplayableUsesTheBodyTheCallerAlreadyHolds(t *testing.T) {
	t.Parallel()

	body := []byte(`{"model":"llama-3-70b","messages":[]}`)
	req := gatewayShapedRequest(t, "/v1/chat/completions", "ignored")
	Replayable(req, body)

	if req.GetBody == nil {
		t.Fatal("Replayable left the request unreplayable")
	}
	if req.ContentLength != int64(len(body)) {
		t.Errorf("ContentLength is %d, want %d", req.ContentLength, len(body))
	}
	for attempt := 0; attempt < 2; attempt++ {
		reader, err := req.GetBody()
		if err != nil {
			t.Fatalf("attempt %d: GetBody: %v", attempt, err)
		}
		got, err := io.ReadAll(reader)
		if err != nil {
			t.Fatalf("attempt %d: reading the replayed body: %v", attempt, err)
		}
		if !bytes.Equal(got, body) {
			t.Errorf("attempt %d gave %q, want %q", attempt, got, body)
		}
	}

	// A body that fails on the first read: makeReplayable must not touch it, since
	// the request is already replayable.
	req.Body = io.NopCloser(failingReader{})
	if err := makeReplayable(req); err != nil {
		t.Errorf("makeReplayable read a body that was already replayable: %v", err)
	}
}

// TestAnOversizedBodyIsRefusedNotTruncated closes a landmine in makeReplayable.
//
// With no declared ContentLength the body has to be read to know its size, and
// reading it under a LimitReader used to leave the pool holding the first cap
// bytes and quietly dropping the rest. A truncated prompt is not a failed
// request: it is a different question, answered confidently, and charged for. It
// is unreachable from the gateway today because the gateway caps requests at the
// same size — which is exactly the kind of reasoning that stops being true.
func TestAnOversizedBodyIsRefusedNotTruncated(t *testing.T) {
	t.Parallel()

	const limit = 1 << 12

	t.Run("a body exactly at the limit is kept whole", func(t *testing.T) {
		t.Parallel()
		body := strings.Repeat("a", limit)
		req := gatewayShapedRequest(t, "/v1/chat/completions", body)
		req.ContentLength = -1
		if err := replayableWithin(req, limit); err != nil {
			t.Fatalf("a body at the limit was refused: %v", err)
		}
		if req.GetBody == nil {
			t.Fatal("a body at the limit was not made replayable")
		}
		if req.ContentLength != limit {
			t.Errorf("ContentLength is %d, want %d", req.ContentLength, limit)
		}
		got, err := io.ReadAll(req.Body)
		if err != nil {
			t.Fatal(err)
		}
		if len(got) != limit {
			t.Errorf("body is %d bytes, want %d", len(got), limit)
		}
	})

	t.Run("an undeclared body over the limit is refused", func(t *testing.T) {
		t.Parallel()
		req := gatewayShapedRequest(t, "/v1/chat/completions", strings.Repeat("a", limit+1))
		req.ContentLength = -1
		err := replayableWithin(req, limit)
		if err == nil {
			t.Fatal("a body over the limit was accepted, which means it was truncated")
		}
		if !errors.Is(err, ErrBodyTooLarge) {
			t.Errorf("failed with %v, want ErrBodyTooLarge", err)
		}
	})

	t.Run("a declared length over the limit is refused without reading", func(t *testing.T) {
		t.Parallel()
		req := gatewayShapedRequest(t, "/v1/chat/completions", "short")
		req.Body = io.NopCloser(failingReader{})
		req.ContentLength = maxReplayBytes + 1
		err := makeReplayable(req)
		if err == nil {
			t.Fatal("a request declaring more than maxReplayBytes was accepted")
		}
		if !errors.Is(err, ErrBodyTooLarge) {
			t.Errorf("failed with %v, want ErrBodyTooLarge", err)
		}
	})
}

// TestAnOversizedBodyNeverReachesACluster is the same refusal seen from the
// exported RoundTripper, so the cap is known to be wired to something.
func TestAnOversizedBodyNeverReachesACluster(t *testing.T) {
	t.Parallel()

	reached := make(chan struct{}, 1)
	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case reached <- struct{}{}:
		default:
		}
		w.WriteHeader(http.StatusOK)
	}), Limits{})

	pool := newPool(t, PoolConfig{})
	link(t, pool, "cluster-ap", connector)

	req := gatewayShapedRequest(t, "/v1/chat/completions", "short")
	req.ContentLength = maxReplayBytes + 1
	resp, err := pool.RoundTripper("cluster-ap").RoundTrip(req)
	if err == nil {
		_ = resp.Body.Close()
		t.Fatal("an oversized request was sent down a tunnel")
	}
	if !errors.Is(err, ErrBodyTooLarge) {
		t.Fatalf("failed with %v, want ErrBodyTooLarge", err)
	}
	select {
	case <-reached:
		t.Error("the cluster saw an oversized request")
	case <-time.After(100 * time.Millisecond):
	}
}

// failingReader is a request body that cannot be read, so a test can tell whether
// something read it.
type failingReader struct{}

func (failingReader) Read([]byte) (int, error) {
	return 0, errors.New("this body must not be read")
}
