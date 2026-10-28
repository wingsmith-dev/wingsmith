package tunnel

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestConnectorRolloutUnderLoad rolls every connector serving a cluster while
// the cluster is carrying traffic, and requires that no caller notices.
//
// The connector is a Deployment on the data path and gets rolled for an image
// bump like anything else. If that costs a burst of 502s, a routine deploy has
// become an inference outage.
func TestConnectorRolloutUnderLoad(t *testing.T) {
	t.Parallel()

	const (
		tunnelsPerCluster = 3
		concurrent        = 40
		generations       = 3
	)

	handler := func(who string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, err := io.ReadAll(r.Body)
			if err != nil || !strings.Contains(string(body), `"model"`) {
				http.Error(w, "body did not survive", http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			flusher := http.NewResponseController(w)
			for i := 0; i < 3; i++ {
				fmt.Fprintf(w, "data: tok%d\n\n", i)
				_ = flusher.Flush()
				time.Sleep(2 * time.Millisecond)
			}
			fmt.Fprintf(w, "data: {\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":3}}\n\ndone-%s\n", who)
		})
	}

	pool := newPool(t, PoolConfig{})
	transport := pool.RoundTripper("cluster-ap")

	type generation struct {
		connector *Connector
		name      string
	}
	live := make([]generation, 0, tunnelsPerCluster+generations)
	for i := 0; i < tunnelsPerCluster; i++ {
		name := fmt.Sprintf("gen0-%d", i)
		connector := newConnector(t, handler(name), Limits{})
		link(t, pool, "cluster-ap", connector)
		live = append(live, generation{connector, name})
	}

	var sent, ok atomic.Int64
	failures := make(chan string, 4096)
	stop := make(chan struct{})

	var wg sync.WaitGroup
	for worker := 0; worker < concurrent; worker++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				sent.Add(1)
				if err := oneCompletion(transport, ""); err != nil {
					failures <- err.Error()
					return
				}
				ok.Add(1)
			}
		}()
	}

	// A rolling update: bring the replacement up, wait for its tunnel to be
	// offered, then drain the one it replaces. Never the other way round, which
	// would leave the cluster with nothing to route to for as long as it took.
	for gen := 1; gen <= generations; gen++ {
		name := fmt.Sprintf("gen%d", gen)
		replacement := newConnector(t, handler(name), Limits{})
		before := pool.Live("cluster-ap")
		link(t, pool, "cluster-ap", replacement)
		eventually(t, patience(t), "the replacement tunnel to be offered", func() bool {
			return pool.Live("cluster-ap") > before
		})

		retiring := live[0]
		live = append(live[1:], generation{replacement, name})
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		if err := retiring.connector.Shutdown(ctx); err != nil {
			t.Errorf("draining %s: %v", retiring.name, err)
		}
		cancel()
	}

	const minimumLoad = 400
	eventually(t, patience(t), "the load generators to have done enough work to mean anything", func() bool {
		return sent.Load() >= minimumLoad || len(failures) > 0
	})
	close(stop)
	wg.Wait()
	close(failures)

	for failure := range failures {
		t.Errorf("a request failed during a connector rollout: %s", failure)
	}
	if sent.Load() < minimumLoad {
		t.Fatalf("only %d requests were sent; the rollout was not under load", sent.Load())
	}
	if ok.Load() != sent.Load() {
		t.Errorf("%d of %d requests completed", ok.Load(), sent.Load())
	}
	t.Logf("%d requests across %d connector generations, all served", sent.Load(), generations+1)
}

// TestConnectorFleetKilledUnderLoad is the same rollout with no manners: every
// connector is SIGKILLed in turn, not drained.
//
// A pod that is deleted gets a TERM and a grace period; a pod that is OOMKilled,
// evicted, or whose node goes away does not, and that is the case a data-plane
// component has to survive because it is the case nobody schedules. The socket is
// reset, the GOAWAY that would have said "I processed nothing" is never sent, and
// x/net's own retry does not recognise the error that arrives instead.
//
// What it asserts is narrower than the graceful case, and intentionally so. A
// request the killed connector was holding may already have been forwarded, so
// it fails as churn instead of being run twice, and a request whose first bytes
// already reached its caller cannot be retried by anyone. Both are counted and
// tolerated. What must hold is that no request is ever served twice, that every
// failure is churn, and that the cluster keeps serving through the tunnels that
// remain.
func TestConnectorFleetKilledUnderLoad(t *testing.T) {
	t.Parallel()

	const (
		tunnelsPerCluster = 3
		concurrent        = 40
		generations       = 3
		minimumLoad       = 400
	)

	served := newCounter()
	handler := func(who string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, err := io.ReadAll(r.Body)
			if err != nil || !strings.Contains(string(body), `"model"`) {
				http.Error(w, "body did not survive", http.StatusBadRequest)
				return
			}
			served.add(r.Header.Get("X-Request-Id"))
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			flusher := http.NewResponseController(w)
			for i := 0; i < 3; i++ {
				fmt.Fprintf(w, "data: tok%d\n\n", i)
				_ = flusher.Flush()
				time.Sleep(2 * time.Millisecond)
			}
			fmt.Fprintf(w, "data: {\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":3}}\n\ndone-%s\n", who)
		})
	}

	pool := newPool(t, PoolConfig{})
	transport := pool.RoundTripper("cluster-ap")

	type tunnel struct {
		conn *Conn
		kill func()
	}
	live := make([]tunnel, 0, tunnelsPerCluster+generations)
	for i := 0; i < tunnelsPerCluster; i++ {
		connector := newConnector(t, handler(fmt.Sprintf("gen0-%d", i)), Limits{})
		conn, kill := linkKillable(t, pool, "cluster-ap", connector)
		live = append(live, tunnel{conn, kill})
	}

	var sent, ok, severed, lost atomic.Int64
	failures := make(chan string, 4096)
	stop := make(chan struct{})

	var wg sync.WaitGroup
	for worker := 0; worker < concurrent; worker++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				id := fmt.Sprintf("req-%d", sent.Add(1))
				err := oneCompletion(transport, id)
				var cut severedError
				switch {
				case err == nil:
					ok.Add(1)
				case errors.As(err, &cut):
					severed.Add(1)
					if cut.fault != FaultChurn {
						failures <- fmt.Sprintf("a severed body classified as %s, not tunnel churn: %v", cut.fault, err)
						return
					}
				case Classify(err) == FaultChurn:
					// Held by the connector when it was killed, and not resent.
					lost.Add(1)
				default:
					failures <- err.Error()
					return
				}
			}
		}()
	}

	// The replacement comes up before the victim is killed, so the cluster is never
	// without coverage: the one thing a rollout controls even when the pods it is
	// replacing do not stop politely.
	for gen := 1; gen <= generations; gen++ {
		replacement := newConnector(t, handler(fmt.Sprintf("gen%d", gen)), Limits{})
		before := pool.Live("cluster-ap")
		conn, kill := linkKillable(t, pool, "cluster-ap", replacement)
		eventuallyCount(t, patience(t), "the replacement tunnel to be offered", before+1,
			func() int { return pool.Live("cluster-ap") })
		live = append(live, tunnel{conn, kill})

		victim := live[0]
		live = live[1:]
		victim.kill()
		select {
		case <-victim.conn.Done():
		case <-time.After(patience(t)):
			t.Fatal("the gateway never noticed a tunnel whose connector was killed")
		}
		eventuallyCount(t, patience(t), "the killed tunnel to leave the pool", tunnelsPerCluster,
			func() int { return pool.Live("cluster-ap") })

		// A rollout is paced by the thing being rolled: the next pod is not killed
		// until the cluster is serving again through the ones that remain. Killing
		// three connectors inside a few milliseconds is not a rollout, it is a
		// cluster-wide outage: one request can then meet every tunnel it has as it
		// dies, and no retry budget covers that because there is nowhere left to go.
		serving := ok.Load() + concurrent
		eventually(t, patience(t), "the cluster to serve again through the surviving tunnels", func() bool {
			return ok.Load() >= serving || len(failures) > 0
		})
	}

	eventually(t, patience(t), "the load generators to have done enough work to mean anything", func() bool {
		return sent.Load() >= minimumLoad || len(failures) > 0
	})
	close(stop)
	wg.Wait()
	close(failures)

	for failure := range failures {
		t.Errorf("a request failed for a reason other than its tunnel being killed: %s", failure)
	}
	if sent.Load() < minimumLoad {
		t.Fatalf("only %d requests were sent; the kills were not under load", sent.Load())
	}
	if ok.Load()+severed.Load()+lost.Load() != sent.Load() {
		t.Errorf("%d of %d requests completed, %d were severed mid-body, %d lost with their tunnel",
			ok.Load(), sent.Load(), severed.Load(), lost.Load())
	}
	for id, times := range served.snapshot() {
		if times > 1 {
			t.Errorf("%s was served %d times", id, times)
		}
	}
	t.Logf("%d requests across %d killed connectors: %d served, %d severed mid-body, %d lost with their tunnel",
		sent.Load(), generations, ok.Load(), severed.Load(), lost.Load())
}

// severedError is a response that had already begun reaching its caller when the
// tunnel under it died.
//
// It is the one failure a killed connector can cause that nothing may fix:
// replaying the request would splice a second generation onto an answer the caller
// is already reading. Kept separate from every other error so a test can tolerate
// this and only this.
type severedError struct {
	fault Fault
	err   error
}

func (e severedError) Error() string { return "severed mid-body: " + e.err.Error() }

func (e severedError) Unwrap() error { return e.err }

// oneCompletion sends one streaming completion and reads it to the end.
func oneCompletion(transport http.RoundTripper, id string) error {
	req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions",
		strings.NewReader(`{"model":"gpt-oss-120b","stream":true}`))
	if err != nil {
		return err
	}
	if id != "" {
		req.Header.Set("X-Request-Id", id)
	}
	resp, err := transport.RoundTrip(req)
	if err != nil {
		return fmt.Errorf("%w (%s)", err, Classify(err))
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return severedError{fault: Classify(err), err: fmt.Errorf("reading body: %w", err)}
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("status %d: %s", resp.StatusCode, body)
	}
	if !strings.Contains(string(body), "tok2") || !strings.Contains(string(body), `"usage"`) {
		return fmt.Errorf("truncated body %q", body)
	}
	return nil
}

// patience is how long a load test waits for something it is sure will happen.
//
// It is the time left before the test binary's own deadline, less a margin to
// report in, and not a fixed wall-clock number. Under -race with every test
// running in parallel a tunnel takes far longer to notice a dead peer than it
// does alone, and a fixed bound turned that slowness into a failure.
func patience(t *testing.T) time.Duration {
	t.Helper()
	deadline, ok := t.Deadline()
	if !ok {
		return 2 * time.Minute
	}
	left := time.Until(deadline) - 10*time.Second
	if left < 10*time.Second {
		return 10 * time.Second
	}
	return left
}
