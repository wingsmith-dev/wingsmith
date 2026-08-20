package serving

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
)

// headerRequestID is the one id a request carries end to end: returned to the caller on every
// response, written on the access log line, and forwarded to the cluster, where Dynamo's frontend
// attaches `x-request-id` to its own trace and log context.
//
// It is not `x-dynamo-request-id`. That header is the runtime's own, is stripped as steering (see
// steering.go), and is deprecated upstream in favour of server-generated ids.
const headerRequestID = "X-Request-Id"

// maxRequestIDLen bounds a caller-supplied id. Long enough for a UUID, a ULID or a W3C trace id
// with a prefix; short enough that a caller cannot use the header to write into our logs.
const maxRequestIDLen = 128

// requestIDFor returns the id this request is known by: the caller's own when it is sane, and a
// fresh one otherwise.
//
// A caller's id is honoured because it is what their own logs already carry, and a support
// conversation goes faster when both sides search for the same string. "Sane" is a closed alphabet
// and a length cap, because the value reaches a log line and an upstream header verbatim.
func requestIDFor(r *http.Request) string {
	if id := r.Header.Get(headerRequestID); saneRequestID(id) {
		return id
	}
	return newRequestID()
}

func saneRequestID(id string) bool {
	if id == "" || len(id) > maxRequestIDLen {
		return false
	}
	for i := 0; i < len(id); i++ {
		c := id[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9':
		case c == '-', c == '_', c == '.', c == ':':
		default:
			return false
		}
	}
	return true
}

// newRequestID is a random UUIDv4, the shape every client library and log query already expects.
func newRequestID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	var out [36]byte
	hex.Encode(out[0:8], b[0:4])
	out[8] = '-'
	hex.Encode(out[9:13], b[4:6])
	out[13] = '-'
	hex.Encode(out[14:18], b[6:8])
	out[18] = '-'
	hex.Encode(out[19:23], b[8:10])
	out[23] = '-'
	hex.Encode(out[24:], b[10:])
	return string(out[:])
}
