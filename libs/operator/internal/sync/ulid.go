package sync

import (
	"crypto/rand"
	"time"
)

// crockford is the Crockford base32 alphabet ULIDs are encoded in.
const crockford = "0123456789abcdefghjkmnpqrstvwxyz"

// NewULID returns a 26-character ULID: 48 bits of millisecond timestamp followed by 80 bits of
// randomness. Frame ids are lexicographically sortable by creation time, which is what makes a
// captured stream readable without reference to the envelope's ts.
func NewULID() string {
	var raw [16]byte
	ms := uint64(time.Now().UTC().UnixMilli())
	for i := 5; i >= 0; i-- {
		raw[i] = byte(ms)
		ms >>= 8
	}
	_, _ = rand.Read(raw[6:])
	return ULIDBody(raw)
}

// ULIDBody encodes 16 bytes as the 26-character Crockford base32 body of a ULID.
//
// It is exported because observed ids share this encoding: an id the cluster mints is a prefix
// plus a body of exactly this shape, and two encoders would eventually disagree about one.
func ULIDBody(raw [16]byte) string {
	var bits uint64
	var nbits uint
	out := make([]byte, 0, 26)
	// The 130-bit encoding space is two bits wider than the value, so the first symbol is
	// derived from the top three bits and the remainder is a plain 5-bit regrouping.
	out = append(out, crockford[raw[0]>>5])
	bits, nbits = uint64(raw[0]&0x1f), 5
	for _, b := range raw[1:] {
		bits = bits<<8 | uint64(b)
		nbits += 8
		for nbits >= 5 {
			nbits -= 5
			out = append(out, crockford[(bits>>nbits)&0x1f])
		}
	}
	return string(out)
}
