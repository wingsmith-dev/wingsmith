package tunnel

import (
	"context"
	"net"
	"sync"
	"testing"
	"time"
)

// TestOneSourceCannotHoldEveryHandshakeSlot is the cheap lockout: dials that
// never finish their handshake used to be able to fill every slot from one
// address, after which the accept loop stopped and real agents waited in the
// kernel backlog.
func TestOneSourceCannotHoldEveryHandshakeSlot(t *testing.T) {
	t.Parallel()

	verifier := &blockingVerifier{release: make(chan struct{})}
	dials := &dialLog{}
	pool, _, addr := startListener(t, ListenerConfig{
		Verifier:               verifier,
		Dials:                  dials,
		MaxHandshakes:          8,
		MaxHandshakesPerSource: 2,
	})

	const attempts = 5
	results := make(chan error, attempts)
	for i := 0; i < attempts; i++ {
		go func() {
			_, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong"))
			results <- err
		}()
	}
	refused := 0
	for i := 0; i < attempts-2; i++ {
		if err := <-results; err != nil {
			refused++
		}
	}
	if refused != attempts-2 {
		t.Fatalf("%d dials refused while two were verifying, want %d", refused, attempts-2)
	}
	if got := verifier.peak(); got > 2 {
		t.Fatalf("%d handshakes from one source were verifying at once, want at most 2", got)
	}
	if got := dials.last(); got != ReasonThrottled {
		t.Errorf("reason = %q, want %q", got, ReasonThrottled)
	}

	close(verifier.release)
	for i := 0; i < 2; i++ {
		if err := <-results; err != nil {
			t.Errorf("a dial inside the bound was refused: %v", err)
		}
	}
	eventually(t, 5*time.Second, "the two tunnels", func() bool { return pool.Live("clu_ap") == 2 })

	// The slots are given back once a dial is a tunnel.
	if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong")); err != nil {
		t.Fatalf("a dial after the others settled was refused: %v", err)
	}
}

// TestASourceThatKeepsFailingIsNotPassedToThePlane pins the negative side of
// the cache. Junk tokens from one address stop reaching the plane, so they
// cannot spend the plane's failed-guess budget, and an agent whose token was
// verified recently still gets in from that address.
func TestASourceThatKeepsFailingIsNotPassedToThePlane(t *testing.T) {
	t.Parallel()

	verifier := &stubVerifier{clusters: map[string]string{"tok-ap": "clu_ap", "tok-us": "clu_us"}}
	dials := &dialLog{}
	pool, _, addr := startListener(t, ListenerConfig{
		Verifier:       verifier,
		Dials:          dials,
		CacheTTL:       time.Hour,
		SourceFailures: 3,
	})

	if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong")); err != nil {
		t.Fatalf("first dial: %v", err)
	}
	for i := 0; i < 3; i++ {
		if _, err := dialConnector(t, addr, Hello{Token: "junk"}, echoHandler("pong")); err == nil {
			t.Fatal("a junk token was accepted")
		}
	}
	asked := verifier.asked()
	if asked != 4 {
		t.Fatalf("the plane was asked %d times, want 4", asked)
	}

	if _, err := dialConnector(t, addr, Hello{Token: "junk"}, echoHandler("pong")); err == nil {
		t.Fatal("a junk token was accepted")
	}
	if got := dials.last(); got != ReasonThrottled {
		t.Errorf("reason = %q, want %q", got, ReasonThrottled)
	}
	if _, err := dialConnector(t, addr, Hello{Token: "tok-us"}, echoHandler("pong")); err == nil {
		t.Fatal("an unverified token from a throttled source was let through")
	}
	if got := verifier.asked(); got != asked {
		t.Fatalf("a throttled source reached the plane: %d calls, want %d", got, asked)
	}

	if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong")); err != nil {
		t.Fatalf("a recently verified token was throttled: %v", err)
	}
	eventually(t, 5*time.Second, "both tunnels", func() bool { return pool.Live("clu_ap") == 2 })
}

// sourceVerifier records the dial source every verification carried.
type sourceVerifier struct {
	mu      sync.Mutex
	sources []string
}

func (v *sourceVerifier) VerifyTunnelToken(ctx context.Context, _ string) (Identity, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.sources = append(v.sources, DialSource(ctx))
	return Identity{ClusterID: "clu_ap"}, nil
}

func (v *sourceVerifier) seen() []string {
	v.mu.Lock()
	defer v.mu.Unlock()
	return append([]string(nil), v.sources...)
}

// TestEveryVerificationCarriesTheAgentsAddress pins what the plane keys its
// budget on. Without it every check shares the gateway's own budget, and
// anyone able to fail dials spends it for the re-verification of tunnels that
// belong to somebody else.
func TestEveryVerificationCarriesTheAgentsAddress(t *testing.T) {
	t.Parallel()

	verifier := &sourceVerifier{}
	_, _, addr := startListener(t, ListenerConfig{
		Verifier:         verifier,
		CacheTTL:         time.Nanosecond,
		ReverifyInterval: 10 * time.Millisecond,
	})
	if _, err := dialConnector(t, addr, Hello{Token: "tok-ap"}, echoHandler("pong")); err != nil {
		t.Fatalf("dial: %v", err)
	}
	eventually(t, 5*time.Second, "a re-verification", func() bool { return len(verifier.seen()) >= 2 })
	for i, source := range verifier.seen() {
		if source != "127.0.0.1" {
			t.Errorf("verification %d carried source %q, want 127.0.0.1", i, source)
		}
	}
}

func TestSourceOfKeysIPv6ByItsSlash64(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct {
		addr net.Addr
		want string
	}{
		{&net.TCPAddr{IP: net.ParseIP("203.0.113.7"), Port: 4242}, "203.0.113.7"},
		{&net.TCPAddr{IP: net.ParseIP("::ffff:203.0.113.7"), Port: 4242}, "203.0.113.7"},
		{&net.TCPAddr{IP: net.ParseIP("2001:db8:1:2:3:4:5:6"), Port: 4242}, "2001:db8:1:2::/64"},
		{nil, ""},
	} {
		if got := sourceOf(tc.addr); got != tc.want {
			t.Errorf("sourceOf(%v) = %q, want %q", tc.addr, got, tc.want)
		}
	}
}

func TestSourceFailuresAgeOutOfTheirWindow(t *testing.T) {
	t.Parallel()

	now := time.Unix(0, 0)
	guard := newSourceGuard(DefaultMaxHandshakesPerSource, 2, time.Minute, func() time.Time { return now })
	guard.fail("a")
	guard.fail("a")
	if !guard.throttled("a") {
		t.Fatal("two failures within the window did not throttle")
	}
	if guard.throttled("b") {
		t.Fatal("one source's failures throttled another")
	}
	now = now.Add(61 * time.Second)
	if guard.throttled("a") {
		t.Fatal("failures older than the window still throttle")
	}
}

func TestNegativeSourceLimitsAreOff(t *testing.T) {
	t.Parallel()

	guard := newSourceGuard(-1, -1, time.Minute, time.Now)
	for i := 0; i < 100; i++ {
		if !guard.enter("a") {
			t.Fatal("a disabled in-flight bound refused a dial")
		}
		guard.fail("a")
	}
	if guard.throttled("a") {
		t.Fatal("a disabled failure limit throttled")
	}
}
