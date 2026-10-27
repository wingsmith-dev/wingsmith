package tunnel

import (
	"context"
	"errors"
	"net"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

// flakyListener fails its first few Accepts with a temporary error, the way a
// process out of file descriptors does, then behaves.
type flakyListener struct {
	net.Listener
	failures atomic.Int32
}

func (l *flakyListener) Accept() (net.Conn, error) {
	if l.failures.Add(-1) >= 0 {
		return nil, &net.OpError{Op: "accept", Net: "tcp", Err: syscall.EMFILE}
	}
	return l.Listener.Accept()
}

// TestATemporaryAcceptErrorDoesNotStopTheListener pins the backoff. The gateway
// treats Serve returning as fatal, so returning on EMFILE turned a burst of
// dials into a gateway restart.
func TestATemporaryAcceptErrorDoesNotStopTheListener(t *testing.T) {
	t.Parallel()

	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap"}}
	pool := newPool(t, PoolConfig{})
	listener, err := NewListener(ListenerConfig{Pool: pool, Verifier: verifier})
	if err != nil {
		t.Fatal(err)
	}
	inner, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	flaky := &flakyListener{Listener: inner}
	flaky.failures.Store(3)

	served := make(chan error, 1)
	go func() { served <- listener.Serve(context.Background(), flaky) }()
	t.Cleanup(func() {
		_ = listener.Close()
		if err := <-served; err != nil {
			t.Errorf("serve: %v", err)
		}
	})

	if _, err := dialConnector(t, inner.Addr().String(), Hello{Token: "tok-ap"}, echoHandler("pong")); err != nil {
		t.Fatalf("a dial after temporary accept errors was refused: %v", err)
	}
	eventually(t, 5*time.Second, "the tunnel", func() bool { return pool.Live("clu_ap") == 1 })
}

// blockingVerifier holds every verification until released, and records how
// many were ever in progress at once.
type blockingVerifier struct {
	release chan struct{}
	mu      sync.Mutex
	now     int
	most    int
}

func (v *blockingVerifier) VerifyTunnelToken(ctx context.Context, _ string) (Identity, error) {
	v.mu.Lock()
	v.now++
	if v.now > v.most {
		v.most = v.now
	}
	v.mu.Unlock()
	defer func() {
		v.mu.Lock()
		v.now--
		v.mu.Unlock()
	}()
	select {
	case <-v.release:
		return Identity{ClusterID: "clu_ap"}, nil
	case <-ctx.Done():
		return Identity{}, ctx.Err()
	}
}

func (v *blockingVerifier) peak() int {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.most
}

// TestHandshakesInProgressAreBounded pins MaxHandshakes: dials past the bound
// wait to be accepted instead of each taking a goroutine at once.
func TestHandshakesInProgressAreBounded(t *testing.T) {
	t.Parallel()

	verifier := &blockingVerifier{release: make(chan struct{})}
	pool, _, addr := startListener(t, ListenerConfig{Verifier: verifier, MaxHandshakes: 2})

	const dials = 5
	results := make(chan error, dials)
	for i := 0; i < dials; i++ {
		go func() {
			_, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong"))
			results <- err
		}()
	}
	eventually(t, 5*time.Second, "two handshakes to be verifying", func() bool { return verifier.peak() == 2 })
	time.Sleep(100 * time.Millisecond)
	if got := verifier.peak(); got != 2 {
		t.Fatalf("%d handshakes were verifying at once, want at most 2", got)
	}
	close(verifier.release)
	for i := 0; i < dials; i++ {
		if err := <-results; err != nil {
			t.Errorf("dial %d: %v", i, err)
		}
	}
	eventually(t, 5*time.Second, "every dial to become a tunnel", func() bool { return pool.Live("clu_ap") == dials })
}

// TestServeOnAClosedListenerSaysSo pins the listener's own sentinel.
func TestServeOnAClosedListenerSaysSo(t *testing.T) {
	t.Parallel()

	listener, err := NewListener(ListenerConfig{Pool: newPool(t, PoolConfig{}), Verifier: &stubVerifier{}})
	if err != nil {
		t.Fatal(err)
	}
	_ = listener.Close()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	if err := listener.Serve(context.Background(), ln); !errors.Is(err, ErrListenerClosed) {
		t.Fatalf("Serve after Close = %v, want ErrListenerClosed", err)
	}
}
