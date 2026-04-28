package tunnel

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"testing"
	"time"
)

func TestReadHelloLeavesTheConnectionOnTheFirstByteAfterTheFrame(t *testing.T) {
	gatewaySide, connectorSide := socketPair(t)

	go func() {
		_, _ = Handshake(context.Background(), connectorSide, Hello{Token: "t", ClusterID: "clu_ap"})
		// Whatever the connector sends next is HTTP/2's business, and the
		// handshake reader must not have eaten any of it.
		_, _ = connectorSide.Write([]byte("PRI * HTTP/2.0"))
	}()

	hello, err := ReadHello(gatewaySide)
	if err != nil {
		t.Fatalf("read hello: %v", err)
	}
	if hello.Token != "t" || hello.ClusterID != "clu_ap" {
		t.Fatalf("hello = %+v", hello)
	}
	if err := WriteAccept(gatewaySide, Accept{OK: true, ClusterID: "clu_ap"}); err != nil {
		t.Fatalf("write accept: %v", err)
	}

	_ = gatewaySide.SetReadDeadline(time.Now().Add(5 * time.Second))
	preface := make([]byte, len("PRI * HTTP/2.0"))
	if _, err := io.ReadFull(gatewaySide, preface); err != nil {
		t.Fatalf("read preface: %v", err)
	}
	if string(preface) != "PRI * HTTP/2.0" {
		t.Fatalf("preface = %q, the handshake reader consumed part of it", preface)
	}
}

func TestUnreadableDialsAreRejected(t *testing.T) {
	oversized := make([]byte, 0, 12)
	oversized = append(oversized, magic...)
	oversized = binary.BigEndian.AppendUint32(oversized, maxHandshakeBytes+1)

	frame := func(body string) []byte {
		out := append([]byte{}, magic...)
		out = binary.BigEndian.AppendUint32(out, uint32(len(body)))
		return append(out, body...)
	}

	cases := []struct {
		name string
		wire []byte
	}{
		{"not a hangar dial", []byte("GET / HTTP/1.1\r\n\r\n")},
		{"truncated", []byte(magic)},
		{"frame past the cap", oversized},
		{"empty frame", frame("")},
		{"not json", frame("{")},
		{"no token", frame(`{"cluster_id":"clu_ap"}`)},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := ReadHello(bytes.NewReader(tc.wire)); err == nil {
				t.Fatal("expected the dial to be refused")
			} else if !errors.Is(err, ErrHandshake) {
				t.Fatalf("err = %v, want ErrHandshake", err)
			}
		})
	}
}
