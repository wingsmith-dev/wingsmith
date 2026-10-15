package tunnel

import (
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

// TestCallersThatStopReadingDoNotFreezeTheTunnel is the shared-window stall.
//
// Unread response bytes hold the connection's flow-control window until
// somebody reads them. Here three responses larger than the connection window
// are left unread, which on its own would leave nothing for any other stream on
// the tunnel. The pool's idle timeout closes them, the window comes back, and a
// small request on the same tunnel completes.
func TestCallersThatStopReadingDoNotFreezeTheTunnel(t *testing.T) {
	t.Parallel()

	limits := Limits{ConnWindow: 8 << 20, StreamWindow: 4 << 20}
	big := strings.Repeat("x", 8<<20)
	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/small" {
			_, _ = w.Write([]byte("small"))
			return
		}
		_, _ = w.Write([]byte(big))
	}), limits)
	pool := newPool(t, PoolConfig{Limits: limits, BodyIdleTimeout: 200 * time.Millisecond})
	link(t, pool, "cluster-a", connector)
	transport := pool.RoundTripper("cluster-a")

	var stuck []*http.Response
	for i := 0; i < 3; i++ {
		req, err := http.NewRequest(http.MethodGet, "https://ignored/big", nil)
		if err != nil {
			t.Fatal(err)
		}
		resp, err := transport.RoundTrip(req)
		if err != nil {
			t.Fatalf("big request %d: %v", i, err)
		}
		stuck = append(stuck, resp)
	}
	t.Cleanup(func() {
		for _, resp := range stuck {
			_ = resp.Body.Close()
		}
	})

	done := make(chan error, 1)
	go func() {
		req, err := http.NewRequest(http.MethodGet, "https://ignored/small", nil)
		if err != nil {
			done <- err
			return
		}
		resp, err := transport.RoundTrip(req)
		if err != nil {
			done <- err
			return
		}
		defer resp.Body.Close()
		body, err := io.ReadAll(resp.Body)
		if err == nil && string(body) != "small" {
			err = errors.New("wrong body " + string(body))
		}
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("small request: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("a small response stayed stuck behind bodies nobody was reading")
	}

	_, err := stuck[0].Body.Read(make([]byte, 1))
	if !errors.Is(err, ErrBodyIdle) {
		t.Errorf("reading an expired body returned %v, want ErrBodyIdle", err)
	}
	if fault := Classify(err); fault != FaultCanceled {
		t.Errorf("an idle body classified as %s, want %s", fault, FaultCanceled)
	}
}

// TestASlowStreamIsNotIdle pins that the timer runs between Reads only: a Read
// waiting on the cluster's next token is the cluster being slow, not the caller.
func TestASlowStreamIsNotIdle(t *testing.T) {
	t.Parallel()

	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		flusher := http.NewResponseController(w)
		for i := 0; i < 3; i++ {
			_, _ = w.Write([]byte("tok "))
			_ = flusher.Flush()
			time.Sleep(300 * time.Millisecond)
		}
	}), Limits{})
	pool := newPool(t, PoolConfig{BodyIdleTimeout: 100 * time.Millisecond})
	link(t, pool, "cluster-a", connector)

	req, err := http.NewRequest(http.MethodGet, "https://ignored/stream", nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := pool.RoundTripper("cluster-a").RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("a slow stream that was being read was cut off: %v", err)
	}
	if string(body) != "tok tok tok " {
		t.Errorf("body %q", body)
	}
}
