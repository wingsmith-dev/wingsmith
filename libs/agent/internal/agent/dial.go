package agent

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"os"
	"time"

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// Dialler opens an authenticated tunnel to one gateway replica.
//
// It is a struct rather than a closure so that the TLS material is read once, at startup: a missing
// CA file should fail the process, not every dial forever.
//
// The handshake itself is tunnel.Handshake — the wire is defined once, in the package both ends
// import, and this half only supplies the identity and the deadline.
type Dialler struct {
	clusterID string
	token     string
	agent     string
	tlsCfg    *tls.Config
	dialer    *net.Dialer
	handshake time.Duration
}

// NewDialler builds the dialler from config.
func NewDialler(cfg *Config) (*Dialler, error) {
	// The pod name, which in a Deployment is the replica's identity. It is for the gateway's logs
	// and nothing else, so a host that will not name itself is not an error.
	name, err := os.Hostname()
	if err != nil {
		name = ""
	}

	d := &Dialler{
		clusterID: cfg.ClusterID,
		token:     cfg.EnrolmentToken,
		agent:     name,
		dialer:    &net.Dialer{Timeout: cfg.DialTimeout},
		handshake: cfg.HandshakeTimeout,
	}
	if !cfg.TLSEnabled {
		return d, nil
	}

	tlsCfg := &tls.Config{
		MinVersion:         tls.VersionTLS12,
		ServerName:         cfg.TLSServerName,
		InsecureSkipVerify: cfg.TLSInsecureSkipVerify, //nolint:gosec
	}
	if cfg.TLSCAFile != "" {
		pem, err := os.ReadFile(cfg.TLSCAFile)
		if err != nil {
			return nil, fmt.Errorf("read HANGAR_TUNNEL_CA_FILE: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("HANGAR_TUNNEL_CA_FILE %s contains no certificates", cfg.TLSCAFile)
		}
		tlsCfg.RootCAs = pool
	}
	d.tlsCfg = tlsCfg
	return d, nil
}

// Dial connects to the gateway, presents the enrolment token, and hands back a connection positioned
// at the first HTTP/2 byte along with which replica answered.
//
// Which replica that is, is not this function's choice — the address is one load-balanced name and
// whatever is in front of it decides. The Peer is how the caller finds out, and the whole of the
// supervisor's coverage logic is built on redialling the same name until every replica has answered
// once.
//
// The returned connection carries no deadline: tunnel.Handshake clears its own, and a deadline left
// on a tunnel would sever whatever generation was running when it expired.
func (d *Dialler) Dial(ctx context.Context, target string) (net.Conn, Peer, error) {
	raw, err := d.dialer.DialContext(ctx, "tcp", target)
	if err != nil {
		return nil, Peer{}, fmt.Errorf("agent: dial %s: %w", target, err)
	}

	conn, err := d.secure(ctx, target, raw)
	if err != nil {
		_ = raw.Close()
		return nil, Peer{}, err
	}

	handshakeCtx, cancel := context.WithTimeout(ctx, d.handshake)
	defer cancel()
	accept, err := tunnel.Handshake(handshakeCtx, conn, tunnel.Hello{
		Token:     d.token,
		ClusterID: d.clusterID,
		Agent:     d.agent,
	})
	if err != nil {
		_ = conn.Close()
		return nil, Peer{}, fmt.Errorf("agent: handshake with %s: %w", target, err)
	}

	// The gateway's answer is the authority on which cluster this token belongs to, and it
	// disagreeing means the Secret in this cluster holds another cluster's credential. Refusing is
	// the only safe move: carrying on would serve that cluster's traffic from this one's GPUs.
	//
	// Either name in the answer counts as agreement. HANGAR_CLUSTER_ID is normally the cluster's
	// name — the chart writes it from what the plane rendered — while the accept's ClusterID is the
	// plane's uid, so comparing against that alone refuses every ordinary install. It refuses none
	// of the dangerous ones: another cluster's credential resolves to a uid *and* a name that are
	// both somebody else's.
	if named := accept.ClusterID != "" || accept.ClusterName != ""; named &&
		accept.ClusterID != d.clusterID && accept.ClusterName != d.clusterID {
		_ = conn.Close()
		return nil, Peer{}, fmt.Errorf("agent: %s resolved this enrolment token to %s (%s), but HANGAR_CLUSTER_ID says %s",
			target, accept.ClusterID, accept.ClusterName, d.clusterID)
	}
	return conn, peerFrom(accept, target), nil
}

// Peer is which gateway replica a dial landed on, and how many the gateway says it is running.
//
// It is a type of this package rather than tunnel.Accept so that the supervisor's coverage logic can
// be tested without building wire frames, and so that the fallbacks for a gateway that did not say
// live in exactly one place.
type Peer struct {
	// ID is the replica's stable identity. Never empty: a gateway that reported none is read as a
	// single replica named by the address, which is correct for one.
	ID string
	// Total is how many replicas the gateway reported, never below one.
	Total int
}

// peerFrom reads the replica identity out of an accept, filling in what an older or single-replica
// gateway leaves out.
//
// The defaults matter more than they look. An agent that read a missing count as zero would hold no
// tunnels at all, and one that read a missing ID as the empty string would file every replica under
// the same key and believe one tunnel covered all of them. Both fall back to "the gateway is one
// replica reachable at this address", which is exactly what an unset field means.
func peerFrom(accept tunnel.Accept, target string) Peer {
	peer := Peer{ID: accept.GatewayID, Total: accept.Gateways}
	if peer.ID == "" {
		peer.ID = target
	}
	if peer.Total < 1 {
		peer.Total = 1
	}
	return peer
}

// secure wraps the connection in TLS unless TLS is off.
func (d *Dialler) secure(ctx context.Context, target string, raw net.Conn) (net.Conn, error) {
	if d.tlsCfg == nil {
		return raw, nil
	}
	cfg := d.tlsCfg
	if cfg.ServerName == "" && !cfg.InsecureSkipVerify {
		host, _, err := net.SplitHostPort(target)
		if err != nil {
			return nil, fmt.Errorf("agent: target %s is not host:port: %w", target, err)
		}
		cfg = cfg.Clone()
		cfg.ServerName = host
	}
	conn := tls.Client(raw, cfg)
	if err := conn.HandshakeContext(ctx); err != nil {
		return nil, fmt.Errorf("agent: TLS handshake with %s: %w", target, err)
	}
	return conn, nil
}
