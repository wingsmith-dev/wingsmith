package serving

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
)

// usageTailBytes is how much of a response is kept to find its usage object.
//
// A fixed window, over bytes that have already been forwarded, is the whole of
// the token accounting here. The alternative — buffering the response — would
// defeat streaming outright, which is the one thing this hop must not do: a
// completion with "stream": true is SSE, and a buffered SSE stream is one long
// pause followed by the entire answer.
const usageTailBytes = 64 << 10

// usageCounts is what an upstream said about its own request.
//
// It is observability and not an invoice. Whoever fronts this gateway meters
// its own users; two systems counting tokens is two systems disagreeing about
// what a customer owes, and this one is deliberately the one without an
// opinion.
type usageCounts struct {
	input       int64
	output      int64
	cachedInput int64
}

// usageTail is a ring over the last bytes of a response.
//
// Every Read passes straight through and the copy into the ring happens after
// the bytes are on their way, so nothing here sits between an upstream token
// and the caller waiting for it.
type usageTail struct {
	src    io.ReadCloser
	buf    []byte
	pos    int
	filled bool
	obs    *observation
	done   bool
}

func newUsageTail(src io.ReadCloser, obs *observation) *usageTail {
	return &usageTail{src: src, buf: make([]byte, usageTailBytes), obs: obs}
}

func (t *usageTail) Read(p []byte) (int, error) {
	n, err := t.src.Read(p)
	if n > 0 {
		t.write(p[:n])
	}
	if err == io.EOF {
		t.finish()
	}
	return n, err
}

func (t *usageTail) Close() error {
	t.finish()
	return t.src.Close()
}

func (t *usageTail) write(b []byte) {
	if len(b) >= len(t.buf) {
		copy(t.buf, b[len(b)-len(t.buf):])
		t.pos = 0
		t.filled = true
		return
	}
	for len(b) > 0 {
		n := copy(t.buf[t.pos:], b)
		b = b[n:]
		t.pos += n
		if t.pos == len(t.buf) {
			t.pos = 0
			t.filled = true
		}
	}
}

// bytes reassembles the window in order.
func (t *usageTail) bytes() []byte {
	if !t.filled {
		return t.buf[:t.pos]
	}
	out := make([]byte, 0, len(t.buf))
	out = append(out, t.buf[t.pos:]...)
	return append(out, t.buf[:t.pos]...)
}

func (t *usageTail) finish() {
	if t.done {
		return
	}
	t.done = true
	if counts, ok := parseUsage(t.bytes()); ok {
		t.obs.recordUsage(counts)
	}
}

// usagePayload is the shape both a completion and a terminal SSE usage chunk
// carry. Pointers throughout: a response that reported nothing must produce no
// token series at all, and a zero is a claim that it reported zero.
type usagePayload struct {
	PromptTokens        *int64 `json:"prompt_tokens"`
	CompletionTokens    *int64 `json:"completion_tokens"`
	PromptTokensDetails *struct {
		CachedTokens *int64 `json:"cached_tokens"`
	} `json:"prompt_tokens_details"`
}

// parseUsage finds the last usage object in a response tail.
//
// The tail rather than the whole body, so the scan is bounded and a long
// generation costs the same as a short one. A body this cannot read — one that
// arrived compressed, or whose usage object is larger than the window — yields
// no counts, and no counts means an absent series rather than a zero one.
func parseUsage(tail []byte) (usageCounts, bool) {
	needle := []byte(`"usage"`)
	for at := bytes.LastIndex(tail, needle); at >= 0; at = bytes.LastIndex(tail[:at], needle) {
		object, ok := objectAfter(tail[at+len(needle):])
		if !ok {
			continue
		}
		var payload usagePayload
		if err := json.Unmarshal(object, &payload); err != nil {
			continue
		}
		if payload.PromptTokens == nil && payload.CompletionTokens == nil {
			continue
		}
		counts := usageCounts{}
		if payload.PromptTokens != nil {
			counts.input = *payload.PromptTokens
		}
		if payload.CompletionTokens != nil {
			counts.output = *payload.CompletionTokens
		}
		if payload.PromptTokensDetails != nil && payload.PromptTokensDetails.CachedTokens != nil {
			counts.cachedInput = *payload.PromptTokensDetails.CachedTokens
		}
		return counts, true
	}
	return usageCounts{}, false
}

// objectAfter returns the JSON object following a key's colon, by matching
// braces. json.Decoder cannot be used here: the window starts mid-document, so
// there is no valid document to decode — only a well-formed object inside one.
func objectAfter(b []byte) ([]byte, bool) {
	i := 0
	for i < len(b) && (b[i] == ' ' || b[i] == '\t' || b[i] == '\n' || b[i] == '\r' || b[i] == ':') {
		i++
	}
	if i >= len(b) || b[i] != '{' {
		return nil, false
	}

	depth := 0
	inString := false
	escaped := false
	for j := i; j < len(b); j++ {
		c := b[j]
		switch {
		case escaped:
			escaped = false
		case c == '\\' && inString:
			escaped = true
		case c == '"':
			inString = !inString
		case inString:
		case c == '{':
			depth++
		case c == '}':
			depth--
			if depth == 0 {
				return b[i : j+1], true
			}
		}
	}
	return nil, false
}

// observeUsage wraps a response body so its tail is read for token counts.
//
// It hangs off the proxy's ModifyResponse, which runs once per response and is
// handed the outbound request — which is a clone of the inbound one and carries
// its context, and therefore its observation.
func observeUsage(resp *http.Response) error {
	if resp.Request == nil || resp.Body == nil {
		return nil
	}
	obs := observationFrom(resp.Request.Context())
	if obs == nil {
		return nil
	}
	resp.Body = newUsageTail(resp.Body, obs)
	return nil
}
