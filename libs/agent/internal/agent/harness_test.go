package agent

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// gateway is a stand-in for the gateway's tunnel listener: it accepts a connection, reads the
// hello, and either rejects it or hands the connection to a callback.
type gateway struct {
	addr string
	ln   net.Listener

	mu     sync.Mutex
	hellos []tunnel.Hello

	// reject, when set, is the reason sent back instead of an acceptance.
	reject string
	// clusterID overrides the cluster the token is said to resolve to. Empty echoes the claim.
	//
	// Echoing is convenient and is also how this harness hid a production bug: a real gateway
	// answers with the plane's uid while an agent claims the cluster's name, so an echo is the one
	// answer no real gateway gives. Set clusterName alongside it to get the real shape.
	clusterID string
	// clusterName is the operator-facing name in the answer, beside the uid above.
	clusterName string
	// token, when set, is the only token accepted.
	token string
	// accepted is called with the connection once the hello has been answered. The callback owns
	// the connection from there.
	accepted func(conn net.Conn)

	// ids, when set, are the replica identities this one listener answers with, cycled per accepted
	// connection. That is a load balancer in front of len(ids) replicas as the agent sees it: one
	// address, and which replica answers is not the dialler's choice.
	ids []string
	// replicas overrides the count reported in the handshake. Zero reports len(ids), or one.
	replicas int
	// accepts counts connections that got as far as being answered, so a test can wait for the
	// cycle to have come round.
	accepts int
}

// identity is the replica identity and count to answer the next dial with.
func (tr *gateway) identity() (string, int) {
	tr.mu.Lock()
	defer tr.mu.Unlock()
	replicas := tr.replicas
	if replicas == 0 {
		replicas = max(len(tr.ids), 1)
	}
	if len(tr.ids) == 0 {
		return "", replicas
	}
	id := tr.ids[tr.accepts%len(tr.ids)]
	tr.accepts++
	return id, replicas
}

// scale changes what the gateway reports from the next dial onwards, standing in for a manifest
// edit and the rollout it causes. The tunnels already open are the caller's to sever.
func (tr *gateway) scale(ids []string) {
	tr.mu.Lock()
	defer tr.mu.Unlock()
	tr.ids = ids
	tr.replicas = len(ids)
	tr.accepts = 0
}

func startGateway(t *testing.T, tr *gateway) *gateway {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	return tr.serveOn(t, ln)
}

func startTLSGateway(t *testing.T, tr *gateway, cert tls.Certificate) *gateway {
	t.Helper()
	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12})
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	return tr.serveOn(t, ln)
}

func (tr *gateway) serveOn(t *testing.T, ln net.Listener) *gateway {
	t.Helper()
	tr.ln = ln
	tr.addr = ln.Addr().String()
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go tr.handle(conn)
		}
	}()
	t.Cleanup(func() { _ = ln.Close() })
	return tr
}

func (tr *gateway) handle(conn net.Conn) {
	hello, err := tunnel.ReadHello(conn)
	if err != nil {
		_ = conn.Close()
		return
	}
	tr.mu.Lock()
	tr.hellos = append(tr.hellos, hello)
	accepted, reject, clusterID, clusterName, token := tr.accepted, tr.reject, tr.clusterID, tr.clusterName, tr.token
	tr.mu.Unlock()

	if token != "" && hello.Token != token {
		reject = "unknown cluster credential"
	}
	if reject != "" {
		_ = tunnel.WriteAccept(conn, tunnel.Accept{Error: reject})
		_ = conn.Close()
		return
	}
	if clusterID == "" {
		clusterID = hello.ClusterID
	}
	id, replicas := tr.identity()
	if err := tunnel.WriteAccept(conn, tunnel.Accept{
		OK:          true,
		ClusterID:   clusterID,
		ClusterName: clusterName,
		GatewayID:   id,
		Gateways:    replicas,
	}); err != nil {
		_ = conn.Close()
		return
	}
	if accepted == nil {
		_ = conn.Close()
		return
	}
	accepted(conn)
}

func (tr *gateway) lastHello(t *testing.T) tunnel.Hello {
	t.Helper()
	tr.mu.Lock()
	defer tr.mu.Unlock()
	if len(tr.hellos) == 0 {
		t.Fatal("gateway saw no hello")
	}
	return tr.hellos[len(tr.hellos)-1]
}

// dialConfig is a config wired at the target, with everything else short.
func dialConfig(target string) *Config {
	return &Config{
		ClusterID:        "clu_test",
		Gateway:          target,
		EnrolmentToken:   "tok_test",
		ConnsPerGateway:  1,
		DialTimeout:      2 * time.Second,
		HandshakeTimeout: 2 * time.Second,
		BackoffMin:       time.Millisecond,
		BackoffMax:       5 * time.Millisecond,
		DrainTimeout:     5 * time.Second,
		HealthAddr:       "127.0.0.1:0",
		LogFormat:        "text",
	}
}

// selfSignedCert issues a certificate for localhost and 127.0.0.1, and writes it as a PEM bundle so
// the CA-file path is what a test verifies against.
func selfSignedCert(t *testing.T) (tls.Certificate, string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	template := x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "gateway.hangar.test"},
		DNSNames:              []string{"gateway.hangar.test", "localhost"},
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create certificate: %v", err)
	}

	path := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o600); err != nil {
		t.Fatalf("write CA: %v", err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, path
}

// eventually polls until cond holds, and fails with why if it never does.
func eventually(t *testing.T, why string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", why)
}
