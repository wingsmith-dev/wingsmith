package agent

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

func TestDialPresentsTheEnrolmentToken(t *testing.T) {
	handed := make(chan net.Conn, 1)
	tr := startGateway(t, &gateway{accepted: func(conn net.Conn) { handed <- conn }})

	cfg := dialConfig(tr.addr)
	cfg.TLSEnabled = false
	dialler, err := NewDialler(cfg)
	if err != nil {
		t.Fatalf("NewDialler: %v", err)
	}

	conn, _, err := dialler.Dial(context.Background(), tr.addr, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	<-handed

	hello := tr.lastHello(t)
	if hello.Token != "tok_test" {
		t.Errorf("token = %q, want tok_test", hello.Token)
	}
	if hello.ClusterID != "clu_test" {
		t.Errorf("cluster = %q, want clu_test", hello.ClusterID)
	}
	if hello.Agent == "" {
		t.Error("hello names no agent, so the gateway's logs cannot say which pod dialled")
	}
}

func TestDialRejectedTokenIsRejected(t *testing.T) {
	tr := startGateway(t, &gateway{token: "the-real-token"})

	cfg := dialConfig(tr.addr)
	cfg.TLSEnabled = false
	dialler, _ := NewDialler(cfg)

	_, _, err := dialler.Dial(context.Background(), tr.addr, nil)
	if !errors.Is(err, tunnel.ErrRejected) {
		t.Fatalf("Dial error = %v, want tunnel.ErrRejected", err)
	}
	if !strings.Contains(err.Error(), "unknown cluster credential") {
		t.Errorf("error %q does not carry the gateway's reason", err)
	}
	if got := classifyDial(err); got != reasonUnauthorized {
		t.Errorf("classifyDial = %q, want %q", got, reasonUnauthorized)
	}
}

// The shape a real handshake has, and the one that refused every install in production: the agent
// claims the cluster's name, because that is what the chart writes HANGAR_CLUSTER_ID from, and the
// gateway answers with the plane's uid, because that is what the pool and every metric label key on.
// Neither side is wrong and the two strings differ, so the answer has to carry both for either side
// to recognise it. Every other test here let the fake gateway echo the claim, which is the one
// answer a real gateway never gives.
func TestDialAcceptsTheUidWhenItClaimedTheName(t *testing.T) {
	tr := startGateway(t, &gateway{clusterID: "clu_ap", clusterName: "cluster-a"})

	cfg := dialConfig(tr.addr)
	cfg.ClusterID = "cluster-a"
	cfg.TLSEnabled = false
	dialler, _ := NewDialler(cfg)

	conn, _, err := dialler.Dial(context.Background(), tr.addr, nil)
	if err != nil {
		t.Fatalf("a dial claiming the cluster's name was refused: %v", err)
	}
	_ = conn.Close()
	if got := tr.lastHello(t).ClusterID; got != "cluster-a" {
		t.Fatalf("claimed %q, want the name the chart configures", got)
	}
}

// A token that resolves to another cluster means the Secret was copied. Serving that cluster's
// traffic from this one's GPUs is worse than not serving it.
// Accepting either name must not have widened this to accepting anything: a copied Secret resolves
// to a uid *and* a name that both belong to somebody else, so neither half matches.
func TestDialRefusesAClusterTheGatewayDisagreesWith(t *testing.T) {
	tr := startGateway(t, &gateway{clusterID: "clu_somebody_else", clusterName: "somebody-else"})

	cfg := dialConfig(tr.addr)
	cfg.TLSEnabled = false
	dialler, _ := NewDialler(cfg)

	_, _, err := dialler.Dial(context.Background(), tr.addr, nil)
	if err == nil {
		t.Fatal("Dial accepted a tunnel for another cluster")
	}
	if !strings.Contains(err.Error(), "HANGAR_CLUSTER_ID") || !strings.Contains(err.Error(), "clu_somebody_else") {
		t.Errorf("error %q does not name the setting and the answer", err)
	}
}

func TestDialGarbageAnswerIsAHandshakeFailure(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		_, _ = conn.Write([]byte("HTTP/1.1 200 OK\r\n\r\n"))
		_ = conn.Close()
	}()

	cfg := dialConfig(ln.Addr().String())
	cfg.TLSEnabled = false
	dialler, _ := NewDialler(cfg)

	_, _, err = dialler.Dial(context.Background(), ln.Addr().String(), nil)
	if !errors.Is(err, tunnel.ErrHandshake) {
		t.Fatalf("Dial error = %v, want tunnel.ErrHandshake", err)
	}
	if got := classifyDial(err); got != reasonHandshake {
		t.Errorf("classifyDial = %q, want %q", got, reasonHandshake)
	}
}

// Whatever the gateway sends the instant it accepts is the start of its HTTP/2 preface, and it has to
// still be there on the connection the dialler hands back.
func TestDialLeavesTheConnectionAtTheFirstByte(t *testing.T) {
	const preface = "PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
	tr := startGateway(t, &gateway{accepted: func(conn net.Conn) {
		_, _ = conn.Write([]byte(preface))
	}})

	cfg := dialConfig(tr.addr)
	cfg.TLSEnabled = false
	dialler, _ := NewDialler(cfg)

	conn, _, err := dialler.Dial(context.Background(), tr.addr, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer func() { _ = conn.Close() }()

	buf := make([]byte, len(preface))
	if err := readFull(conn, buf); err != nil {
		t.Fatalf("read the preface: %v", err)
	}
	if string(buf) != preface {
		t.Errorf("read %q, want %q", buf, preface)
	}
}

// A deadline left on the connection would sever whatever generation was running when it expired.
func TestDialClearsTheHandshakeDeadline(t *testing.T) {
	late := make(chan struct{})
	tr := startGateway(t, &gateway{accepted: func(conn net.Conn) {
		<-late
		_, _ = conn.Write([]byte("x"))
	}})

	cfg := dialConfig(tr.addr)
	cfg.TLSEnabled = false
	cfg.HandshakeTimeout = 30 * time.Millisecond
	dialler, _ := NewDialler(cfg)

	conn, _, err := dialler.Dial(context.Background(), tr.addr, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer func() { _ = conn.Close() }()

	time.Sleep(80 * time.Millisecond)
	close(late)

	// Read with no deadline of its own: setting one would paper over a stale deadline left on the
	// connection, which is the whole thing under test.
	read := make(chan error, 1)
	go func() {
		buf := make([]byte, 1)
		_, err := conn.Read(buf)
		read <- err
	}()
	select {
	case err := <-read:
		if err != nil {
			t.Fatalf("read after the handshake timeout would have fired: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("read never returned")
	}
}

func TestDialOverTLSVerifiesAgainstTheConfiguredCA(t *testing.T) {
	cert, caFile := selfSignedCert(t)
	handed := make(chan net.Conn, 1)
	tr := startTLSGateway(t, &gateway{accepted: func(conn net.Conn) { handed <- conn }}, cert)

	cfg := dialConfig(tr.addr)
	cfg.TLSEnabled = true
	cfg.TLSCAFile = caFile
	// The test dials 127.0.0.1 while the certificate is issued for a name, which is the one case
	// the override is still for.
	cfg.TLSServerName = "gateway.hangar.test"
	dialler, err := NewDialler(cfg)
	if err != nil {
		t.Fatalf("NewDialler: %v", err)
	}

	conn, _, err := dialler.Dial(context.Background(), tr.addr, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	<-handed

	if got := tr.lastHello(t).ClusterID; got != "clu_test" {
		t.Errorf("cluster = %q, want clu_test", got)
	}
}

func TestDialOverTLSRefusesAnUnknownCertificate(t *testing.T) {
	cert, _ := selfSignedCert(t)
	tr := startTLSGateway(t, &gateway{}, cert)

	cfg := dialConfig(tr.addr)
	cfg.TLSEnabled = true
	cfg.TLSServerName = "gateway.hangar.test"
	dialler, _ := NewDialler(cfg)

	_, _, err := dialler.Dial(context.Background(), tr.addr, nil)
	if err == nil {
		t.Fatal("Dial succeeded against a certificate no CA vouches for")
	}
	if !strings.Contains(err.Error(), "TLS handshake") {
		t.Errorf("error %q does not name the handshake", err)
	}
}

func TestNewDiallerRejectsAnUnreadableCA(t *testing.T) {
	cfg := dialConfig("gw-0.gw:9443")
	cfg.TLSEnabled = true
	cfg.TLSCAFile = "/nowhere/ca.pem"
	if _, err := NewDialler(cfg); err == nil || !strings.Contains(err.Error(), "HANGAR_TUNNEL_CA_FILE") {
		t.Fatalf("NewDialler error = %v, want one naming HANGAR_TUNNEL_CA_FILE", err)
	}
}

func readFull(conn net.Conn, buf []byte) error {
	if err := conn.SetReadDeadline(time.Now().Add(2 * time.Second)); err != nil {
		return err
	}
	read := 0
	for read < len(buf) {
		n, err := conn.Read(buf[read:])
		read += n
		if err != nil {
			return err
		}
	}
	return conn.SetReadDeadline(time.Time{})
}

// A gateway that could not reach the plane has not refused this cluster's credential, and counting
// it as unauthorized sent people to check Secrets during a control-plane outage.
func TestAnUnreachablePlaneIsRefusedNotUnauthorized(t *testing.T) {
	tr := startGateway(t, &gateway{reject: tunnel.ReasonUnavailable})

	cfg := dialConfig(tr.addr)
	cfg.TLSEnabled = false
	dialler, _ := NewDialler(cfg)

	_, _, err := dialler.Dial(context.Background(), tr.addr, nil)
	if errors.Is(err, tunnel.ErrRejected) {
		t.Fatalf("Dial error = %v, an unreachable plane read as a rejected token", err)
	}
	if got := classifyDial(err); got != reasonRefused {
		t.Errorf("classifyDial = %q, want %q", got, reasonRefused)
	}
}

// The covered list rides on the hello, and a replica that is on it answers with its identity and
// the replica count rather than a tunnel.
func TestADialToACoveredReplicaReturnsItsPeer(t *testing.T) {
	tr := startGateway(t, &gateway{ids: []string{"hangar-gateway-0"}, replicas: 3, accepted: func(net.Conn) {}})

	cfg := dialConfig(tr.addr)
	cfg.TLSEnabled = false
	dialler, _ := NewDialler(cfg)

	conn, peer, err := dialler.Dial(context.Background(), tr.addr, []string{"hangar-gateway-0"})
	if conn != nil {
		_ = conn.Close()
	}
	if !errors.Is(err, tunnel.ErrDuplicate) {
		t.Fatalf("Dial error = %v, want tunnel.ErrDuplicate", err)
	}
	if peer.ID != "hangar-gateway-0" || peer.Total != 3 {
		t.Errorf("peer = %+v, want hangar-gateway-0 of 3", peer)
	}
	if got := tr.lastHello(t).Covered; len(got) != 1 || got[0] != "hangar-gateway-0" {
		t.Errorf("hello carried covered = %v", got)
	}
}
