package tunnel

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"

	"golang.org/x/net/http2"
)

// timeoutError is a net.Error that reports a timeout, which is the only thing
// the gateway's existing markProxyError looks for.
type timeoutError struct{}

func (timeoutError) Error() string   { return "i/o timeout" }
func (timeoutError) Timeout() bool   { return true }
func (timeoutError) Temporary() bool { return true }

func TestClassify(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name string
		err  error
		want Fault
	}{
		{"nothing went wrong", nil, FaultNone},
		{"no connector attached", fmt.Errorf("%w cluster-ap", ErrNoTunnel), FaultNoTunnel},
		{"request carried no cluster", ErrNoCluster, FaultNoTunnel},
		{"every tunnel full", fmt.Errorf("%w cluster-ap", ErrSaturated), FaultSaturated},
		{"connector drained", http2.GoAwayError{LastStreamID: 7, ErrCode: http2.ErrCodeNo}, FaultChurn},
		{"connector refused a stream", http2.StreamError{StreamID: 9, Code: http2.ErrCodeRefusedStream}, FaultChurn},
		{"connector reset a stream", http2.StreamError{StreamID: 9, Code: http2.ErrCodeInternal}, FaultChurn},
		{"tunnel protocol error", http2.ConnectionError(http2.ErrCodeProtocol), FaultChurn},
		{"tunnel died mid-body", io.ErrUnexpectedEOF, FaultChurn},
		{"connector went away", errors.New("http2: client connection lost"), FaultChurn},
		{"tunnel retired under the request", errors.New("http2: client conn not usable"), FaultChurn},
		{"goaway sentinel escaped the retry budget",
			errors.New("http2: Transport received Server's graceful shutdown GOAWAY"), FaultChurn},
		{"wrapped churn", fmt.Errorf("proxying: %w", http2.GoAwayError{}), FaultChurn},
		{"caller hung up", context.Canceled, FaultCanceled},
		{"caller hung up, wrapped", fmt.Errorf("copying: %w", context.Canceled), FaultCanceled},
		{"request canceled", errors.New("net/http: request canceled"), FaultCanceled},
		{"no response header in time", timeoutError{}, FaultHeaderTimeout},
		{"deadline passed", context.DeadlineExceeded, FaultHeaderTimeout},
		{"the cluster itself failed", errors.New("dial tcp 10.0.0.1:8000: connection refused"), FaultUpstream},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := Classify(testCase.err); got != testCase.want {
				t.Errorf("Classify(%v) = %s, want %s", testCase.err, got, testCase.want)
			}
		})
	}
}

// TestTunnelChurnIsInvisibleToTheExistingClassifier is the reason Classify
// exists, stated as a test rather than as a claim in a doc comment.
//
// gateway.markProxyError asks two questions — is this context.Canceled, is this
// a net.Error that timed out — and calls everything else OutcomeUpstreamError,
// which counts as FAILED and deflates serving availability. Neither http2 error
// answers either question, so a connector redeploy reads as the cluster being
// down. If this test ever fails because x/net made GoAwayError a net.Error, the
// gap has closed and Classify's churn branch can be reconsidered.
func TestTunnelChurnIsInvisibleToTheExistingClassifier(t *testing.T) {
	t.Parallel()

	churn := []error{
		http2.GoAwayError{LastStreamID: 3, ErrCode: http2.ErrCodeNo},
		http2.StreamError{StreamID: 5, Code: http2.ErrCodeRefusedStream},
		errors.New("http2: client connection lost"),
	}
	for _, err := range churn {
		if errors.Is(err, context.Canceled) {
			t.Errorf("%v now reports itself as a cancellation", err)
		}
		var netErr net.Error
		if errors.As(err, &netErr) && netErr.Timeout() {
			t.Errorf("%v now reports itself as a timeout", err)
		}
		if Classify(err) != FaultChurn {
			t.Errorf("%v is not classified as tunnel churn", err)
		}
	}
}

// TestARealClusterFailureIsNotChurn is the other side of the same coin. A cluster
// whose serving runtime is not answering is a genuine failure and has to keep
// reading as one, or the tunnel's classification becomes a way to make every
// outage disappear.
func TestARealClusterFailureIsNotChurn(t *testing.T) {
	t.Parallel()

	// The connector is up; the thing it proxies to is not.
	dead, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := dead.Addr().String()
	if err := dead.Close(); err != nil {
		t.Fatal(err)
	}

	connector := newConnector(t, proxyTo("http://"+address), Limits{})
	pool := newPool(t, PoolConfig{})
	link(t, pool, "cluster-ap", connector)

	req, err := http.NewRequest(http.MethodPost, "https://ignored/v1/chat/completions", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := pool.RoundTripper("cluster-ap").RoundTrip(req)
	if err != nil {
		t.Fatalf("the tunnel itself failed rather than reporting the upstream: %v (%s)", err, Classify(err))
	}
	defer resp.Body.Close()

	// A cluster that cannot serve arrives as a status, not as a transport error:
	// the tunnel worked, and the connector said so. Which is the distinction the
	// availability number needs.
	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("status %d, want %d", resp.StatusCode, http.StatusBadGateway)
	}
	if fault := Classify(nil); fault != FaultNone {
		t.Errorf("a completed round trip classified as %s", fault)
	}
}

// TestFaultRetryability pins which faults describe a request that could be sent
// again, so a pool built on this does not have to guess.
func TestFaultRetryability(t *testing.T) {
	t.Parallel()

	retryable := map[Fault]bool{
		FaultNoTunnel:  true,
		FaultSaturated: true,
		FaultChurn:     true,
	}
	for _, fault := range []Fault{
		FaultNone, FaultNoTunnel, FaultSaturated, FaultChurn,
		FaultHeaderTimeout, FaultCanceled, FaultUpstream,
	} {
		if got := fault.Retryable(); got != retryable[fault] {
			t.Errorf("%s.Retryable() = %v, want %v", fault, got, retryable[fault])
		}
		if fault.String() == "" {
			t.Errorf("fault %d has no label", int(fault))
		}
	}
}
