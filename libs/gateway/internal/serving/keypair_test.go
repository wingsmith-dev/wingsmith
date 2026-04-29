package serving

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// writeKeypair puts a self-signed certificate on disk and returns its paths.
func writeKeypair(t *testing.T, dir string, notAfter time.Time) (string, string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("key: %v", err)
	}
	template := x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: "tunnel.hangar.test"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     notAfter,
		DNSNames:     []string{"tunnel.hangar.test"},
	}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("certificate: %v", err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatalf("marshal key: %v", err)
	}

	certPath := filepath.Join(dir, "tls.crt")
	keyPath := filepath.Join(dir, "tls.key")
	writeFile(t, certPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
	writeFile(t, keyPath, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}))
	return certPath, keyPath
}

func writeFile(t *testing.T, path string, content []byte) {
	t.Helper()
	if err := os.WriteFile(path, content, 0o600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func TestARenewedCertificateIsPickedUpWithoutARestart(t *testing.T) {
	dir := t.TempDir()
	first := time.Now().Add(90 * 24 * time.Hour).Truncate(time.Second)
	certPath, keyPath := writeKeypair(t, dir, first)

	keypair, err := NewKeypair(certPath, keyPath)
	if err != nil {
		t.Fatalf("NewKeypair: %v", err)
	}
	if !keypair.NotAfter().Equal(first) {
		t.Fatalf("NotAfter = %v, want %v", keypair.NotAfter(), first)
	}

	// What cert-manager does 30 days out, and what the process used to ignore.
	renewed := time.Now().Add(180 * 24 * time.Hour).Truncate(time.Second)
	writeKeypair(t, dir, renewed)

	swapped, err := keypair.Reload()
	if err != nil {
		t.Fatalf("Reload: %v", err)
	}
	if !swapped {
		t.Fatal("a renewed certificate on disk did not swap")
	}
	if !keypair.NotAfter().Equal(renewed) {
		t.Fatalf("NotAfter = %v, want the renewed %v", keypair.NotAfter(), renewed)
	}

	served, err := keypair.GetCertificate(nil)
	if err != nil {
		t.Fatalf("GetCertificate: %v", err)
	}
	if !served.Leaf.NotAfter.Equal(renewed) {
		t.Fatal("the handshake callback still answers with the old certificate")
	}
}

func TestAnUnchangedCertificateDoesNotSwap(t *testing.T) {
	dir := t.TempDir()
	certPath, keyPath := writeKeypair(t, dir, time.Now().Add(time.Hour))
	keypair, err := NewKeypair(certPath, keyPath)
	if err != nil {
		t.Fatalf("NewKeypair: %v", err)
	}
	swapped, err := keypair.Reload()
	if err != nil {
		t.Fatalf("Reload: %v", err)
	}
	if swapped {
		t.Fatal("reported a swap with nothing changed, which would log on every tick")
	}
}

func TestAnUnreadableCertificateKeepsTheLoadedOne(t *testing.T) {
	dir := t.TempDir()
	expiry := time.Now().Add(time.Hour).Truncate(time.Second)
	certPath, keyPath := writeKeypair(t, dir, expiry)
	keypair, err := NewKeypair(certPath, keyPath)
	if err != nil {
		t.Fatalf("NewKeypair: %v", err)
	}

	// A read landing while the kubelet is mid-swap. It must not be able to take
	// the tunnel port down — the loaded certificate is still perfectly valid.
	writeFile(t, certPath, []byte("-----BEGIN CERTIFICATE-----\ntruncated"))

	if _, err := keypair.Reload(); err == nil {
		t.Fatal("a truncated certificate must be reported as an error")
	}
	if !keypair.NotAfter().Equal(expiry) {
		t.Fatal("a failed reload discarded the working certificate")
	}
	served, err := keypair.GetCertificate(nil)
	if err != nil || served == nil {
		t.Fatalf("handshakes must keep succeeding after a failed reload: %v", err)
	}
}

func TestAMissingCertificateIsFatalAtStartup(t *testing.T) {
	dir := t.TempDir()
	if _, err := NewKeypair(filepath.Join(dir, "absent.crt"), filepath.Join(dir, "absent.key")); err == nil {
		t.Fatal("startup with no certificate must fail rather than bind without TLS")
	}
}

func TestWatchReportsTheNewExpiryAndStopsWithTheContext(t *testing.T) {
	dir := t.TempDir()
	certPath, keyPath := writeKeypair(t, dir, time.Now().Add(24*time.Hour))
	keypair, err := NewKeypair(certPath, keyPath)
	if err != nil {
		t.Fatalf("NewKeypair: %v", err)
	}

	renewed := time.Now().Add(120 * 24 * time.Hour).Truncate(time.Second)
	writeKeypair(t, dir, renewed)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	seen := make(chan time.Time, 1)
	done := make(chan struct{})
	go func() {
		keypair.Watch(ctx, time.Millisecond, func(at time.Time) { seen <- at })
		close(done)
	}()

	select {
	case at := <-seen:
		if !at.Equal(renewed) {
			t.Fatalf("reported %v, want %v", at, renewed)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Watch never noticed the renewed certificate")
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Watch outlived its context")
	}
}
