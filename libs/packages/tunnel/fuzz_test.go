package tunnel

import (
	"bytes"
	"encoding/binary"
	"errors"
	"reflect"
	"testing"
	"unicode/utf8"
)

// FuzzReadHello feeds the internet-facing parser arbitrary bytes. Whatever
// arrives, it must not panic, must fail as ErrHandshake instead of anything
// else, and on success must have consumed exactly one frame: the bytes after it
// belong to HTTP/2.
func FuzzReadHello(f *testing.F) {
	var valid bytes.Buffer
	if err := writeFrame(&valid, []byte(magic), Hello{Token: "tok", ClusterID: "clu", Covered: []string{"gw-0"}}); err != nil {
		f.Fatal(err)
	}
	f.Add(valid.Bytes(), []byte("PRI * HTTP/2.0\r\n"))
	f.Add([]byte(magic), []byte{})
	f.Add([]byte("GET / HTTP/1.1\r\n\r\n"), []byte{})
	huge := append([]byte(magic), 0xff, 0xff, 0xff, 0xff)
	f.Add(huge, []byte{})
	f.Add(append([]byte(magic), 0, 0, 0, 2, '{', '}'), []byte("rest"))

	f.Fuzz(func(t *testing.T, frame, rest []byte) {
		input := append(append([]byte(nil), frame...), rest...)
		reader := bytes.NewReader(input)
		hello, err := ReadHello(reader)
		if err != nil {
			if !errors.Is(err, ErrHandshake) {
				t.Fatalf("ReadHello failed with %v, not ErrHandshake", err)
			}
			return
		}
		if hello.Token == "" {
			t.Fatal("ReadHello accepted a hello with no token")
		}
		// Exactly magic, a length, and that many bytes were read.
		size := binary.BigEndian.Uint32(input[len(magic) : len(magic)+4])
		if size > maxHandshakeBytes {
			t.Fatalf("accepted a %d byte frame past the %d cap", size, maxHandshakeBytes)
		}
		consumed := len(input) - reader.Len()
		if want := len(magic) + 4 + int(size); consumed != want {
			t.Fatalf("consumed %d bytes, want exactly one frame of %d", consumed, want)
		}
	})
}

// FuzzAcceptRoundTrip pins that whatever the gateway writes, the agent reads
// back unchanged, and that a refusal always maps to one of the three sentinels.
func FuzzAcceptRoundTrip(f *testing.F) {
	f.Add(true, "clu", "name", "", "gw-0", 3)
	f.Add(false, "", "", ReasonUnauthorized, "", 0)
	f.Add(false, "", "", ReasonDuplicate, "gw-1", 2)
	f.Add(false, "", "", "something new", "", 0)

	f.Fuzz(func(t *testing.T, ok bool, clusterID, clusterName, reason, gatewayID string, gateways int) {
		// JSON replaces invalid UTF-8, and every value here comes from a config
		// or the plane as valid text.
		for _, s := range []string{clusterID, clusterName, reason, gatewayID} {
			if !utf8.ValidString(s) {
				return
			}
		}
		sent := Accept{OK: ok, ClusterID: clusterID, ClusterName: clusterName, Reason: reason, Error: reason,
			GatewayID: gatewayID, Gateways: gateways}
		var wire bytes.Buffer
		if err := WriteAccept(&wire, sent); err != nil {
			if errors.Is(err, ErrHandshake) {
				return // past the frame cap, refused on the way out
			}
			t.Fatal(err)
		}
		var got Accept
		if err := readFrame(&wire, nil, &got); err != nil {
			t.Fatalf("reading back what was written: %v", err)
		}
		if !reflect.DeepEqual(got, sent) {
			t.Fatalf("read %+v, wrote %+v", got, sent)
		}
		if ok {
			return
		}
		err := refusal(got)
		if !errors.Is(err, ErrRejected) && !errors.Is(err, ErrDuplicate) && !errors.Is(err, ErrRefused) {
			t.Fatalf("refusal %q mapped to %v", reason, err)
		}
		if errors.Is(err, ErrRejected) != (reason == ReasonUnauthorized) {
			t.Fatalf("reason %q: ErrRejected is %v", reason, errors.Is(err, ErrRejected))
		}
	})
}
