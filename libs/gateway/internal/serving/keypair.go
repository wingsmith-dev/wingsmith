package serving

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"
)

// Keypair holds the tunnel listener's certificate and swaps it when the one on
// disk changes.
//
// It exists because cert-manager renews and this process would not notice. The
// certificate is mounted from a Secret, cert-manager rewrites that Secret 30 days
// before expiry, and the kubelet puts the new bytes on disk within about a
// minute, but a `tls.Config` built with `Certificates` holds what it was given
// at startup for the life of the process. So the gateway would serve the old
// certificate until it expired, and every agent dial would then fail the
// handshake at once: no tunnels, no reachable upstreams, a 503 on every model in
// the fleet instead of a degradation.
//
// It was also self-healing by accident, which is worse than plainly broken. Any
// deploy ends in a rollout restart, as does any node drain, so the outage only
// lands if the gateway is left untouched across the whole 30-day renewal window.
// That is the quiet period when nobody is looking.
type Keypair struct {
	certPath string
	keyPath  string
	current  atomic.Pointer[tls.Certificate]
}

// NewKeypair reads the pair once and fails if it cannot.
//
// Startup is the one time a bad certificate should be fatal: there is nothing to
// fall back to, and a tunnel listener that binds without TLS would take an
// agent's enrolment token in cleartext.
func NewKeypair(certPath, keyPath string) (*Keypair, error) {
	k := &Keypair{certPath: certPath, keyPath: keyPath}
	cert, err := k.read()
	if err != nil {
		return nil, err
	}
	k.current.Store(cert)
	return k, nil
}

// read loads and parses the pair, returning a certificate with Leaf populated.
//
// The leaf is parsed here, not left to the TLS stack because the expiry is
// what makes a reload legible: both the log line and the metric want it, and a
// certificate whose leaf is nil can only be described as "a new one".
func (k *Keypair) read() (*tls.Certificate, error) {
	cert, err := tls.LoadX509KeyPair(k.certPath, k.keyPath)
	if err != nil {
		return nil, fmt.Errorf("load tunnel keypair: %w", err)
	}
	if len(cert.Certificate) == 0 {
		return nil, fmt.Errorf("load tunnel keypair: %s holds no certificate", k.certPath)
	}
	leaf, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil {
		return nil, fmt.Errorf("parse tunnel certificate: %w", err)
	}
	cert.Leaf = leaf
	return &cert, nil
}

// GetCertificate answers every handshake from the currently loaded pair.
//
// Wired as the `tls.Config` callback instead of `Certificates`, which is the
// whole point: the callback is consulted per handshake, so a swap takes effect on
// the next dial without restarting the listener or dropping the tunnels already
// held on it.
func (k *Keypair) GetCertificate(*tls.ClientHelloInfo) (*tls.Certificate, error) {
	if cert := k.current.Load(); cert != nil {
		return cert, nil
	}
	return nil, fmt.Errorf("no tunnel certificate loaded")
}

// NotAfter reports when the loaded certificate expires.
//
// The loaded one, not the one on disk. cert-manager's own metrics
// describe the Secret and would look healthy the day after a renewal this process
// ignored, so the only reading that can catch that is taken from memory.
func (k *Keypair) NotAfter() time.Time {
	if cert := k.current.Load(); cert != nil && cert.Leaf != nil {
		return cert.Leaf.NotAfter
	}
	return time.Time{}
}

// Reload re-reads the pair and swaps it if the certificate differs, reporting
// whether it changed.
//
// Compared on the leaf's DER bytes, not on the expiry, so a reissue that
// happens to carry the same validity window is still picked up.
func (k *Keypair) Reload() (bool, error) {
	next, err := k.read()
	if err != nil {
		return false, err
	}
	current := k.current.Load()
	if current != nil && len(current.Certificate) > 0 && string(current.Certificate[0]) == string(next.Certificate[0]) {
		return false, nil
	}
	k.current.Store(next)
	return true, nil
}

// Watch re-reads the pair on a timer until the context ends, calling changed with
// the new expiry whenever it swaps.
//
// A timer and not a filesystem watch. Kubernetes replaces a Secret volume by
// atomically re-pointing a `..data` symlink, which a watcher reports as events on
// a directory that has ceased to exist: fiddly to get right, and for a window
// measured in days there is nothing to gain by being prompt.
//
// A failed read leaves the working certificate in place and logs. That is the
// important half: a read landing while the kubelet is mid-swap must not be able
// to take down the tunnel port, and the next tick will succeed anyway.
func (k *Keypair) Watch(ctx context.Context, every time.Duration, changed func(time.Time)) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			swapped, err := k.Reload()
			if err != nil {
				slog.Error("gateway: could not reload the tunnel certificate, keeping the loaded one",
					"cert", k.certPath, "expires", k.NotAfter(), "err", err)
				continue
			}
			if !swapped {
				continue
			}
			slog.Info("gateway: tunnel certificate reloaded", "expires", k.NotAfter())
			if changed != nil {
				changed(k.NotAfter())
			}
		}
	}
}
