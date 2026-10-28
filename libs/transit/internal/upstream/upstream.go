// Package upstream is a fake OpenAI-compatible frontend with a schedule instead
// of a model.
//
// It stands where Dynamo stands and answers with the timing a caller would see
// from a real generation: a wait for the first token, then a token every so
// often, then a usage object. Nothing about a response here depends on a GPU,
// so a measurement taken through it is a measurement of the transport and not
// of an engine, which is the only kind of measurement this repository can take
// on a laptop.
package upstream

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"sync/atomic"
	"time"
)

// HeaderReceived carries the wall-clock nanosecond at which this frontend was
// handed the request, and every SSE chunk carries transit_write_ns for the
// moment it was written.
//
// The two stamps are what make a measurement attributable, not inferred.
// A caller that knows when it sent, when the frontend received, when the
// frontend wrote the first token and when that token arrived can split one
// number into the inbound leg (gateway plus tunnel plus agent) and the return
// leg, without any change to the gateway or the agent. They are only meaningful
// where both ends share a clock, which is one host, which is exactly the
// synthetic mode they exist for. Against a real frontend they are absent and
// the report says so instead of guessing.
//
// The gateway forwards only an allowlist of response headers, so the header
// does not survive the path; the same stamp also rides in the body, as
// transit_received_ns on every chunk and on a non-streaming answer.
const HeaderReceived = "X-Transit-Received-Ns"

// Profile is the shape of one generated answer.
type Profile struct {
	// TTFT is how long the frontend holds the stream before the first token.
	TTFT time.Duration
	// ITL is the gap between tokens after the first.
	ITL time.Duration
	// Tokens is how many content chunks are emitted.
	Tokens int
	// TokenBytes is how many bytes of content each chunk carries.
	TokenBytes int
}

// DefaultProfile is a small streaming answer: fast enough that a run finishes,
// slow enough that a stream is a stream.
func DefaultProfile() Profile {
	return Profile{TTFT: 200 * time.Millisecond, ITL: 10 * time.Millisecond, Tokens: 64, TokenBytes: 8}
}

func (p Profile) withDefaults() Profile {
	defaults := DefaultProfile()
	if p.Tokens <= 0 {
		p.Tokens = defaults.Tokens
	}
	if p.TokenBytes <= 0 {
		p.TokenBytes = defaults.TokenBytes
	}
	return p
}

// Server answers completions and counts what it was asked to do.
type Server struct {
	profile Profile

	inflight atomic.Int64
	peak     atomic.Int64
	served   atomic.Int64
	aborted  atomic.Int64
}

// New builds a frontend that answers to profile unless a request overrides it.
func New(profile Profile) *Server {
	return &Server{profile: profile.withDefaults()}
}

// request is the OpenAI body, plus the fields that let one caller in a run ask
// for a different schedule from everyone else.
//
// The overrides are named instead of passed as query parameters, because the
// gateway routes on the body and rewrites it; a parameter on the URL would
// prove nothing about the path a real completion takes.
type request struct {
	Model  string `json:"model"`
	Stream bool   `json:"stream"`
	TTFTMs *int   `json:"wingsmith_ttft_ms"`
	ITLMs  *int   `json:"wingsmith_itl_ms"`
	Tokens *int   `json:"wingsmith_tokens"`
}

// Stats is what the frontend saw, for the report.
type Stats struct {
	Served       int64 `json:"served"`
	Aborted      int64 `json:"aborted"`
	InFlight     int64 `json:"in_flight"`
	PeakInFlight int64 `json:"peak_in_flight"`
}

// Stats reports the counters.
func (s *Server) Stats() Stats {
	return Stats{
		Served:       s.served.Load(),
		Aborted:      s.aborted.Load(),
		InFlight:     s.inflight.Load(),
		PeakInFlight: s.peak.Load(),
	}
}

// Handler is the frontend's mux.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /stats", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(s.Stats())
	})
	mux.HandleFunc("GET /v1/models", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"object":"list","data":[{"id":"transit","object":"model","owned_by":"wingsmith"}]}`))
	})
	mux.HandleFunc("POST /v1/chat/completions", s.complete)
	mux.HandleFunc("POST /v1/completions", s.complete)
	return mux
}

func (s *Server) complete(w http.ResponseWriter, r *http.Request) {
	var body request
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, `{"error":{"message":"bad body"}}`, http.StatusBadRequest)
		return
	}

	profile := s.profile
	if body.TTFTMs != nil {
		profile.TTFT = time.Duration(*body.TTFTMs) * time.Millisecond
	}
	if body.ITLMs != nil {
		profile.ITL = time.Duration(*body.ITLMs) * time.Millisecond
	}
	if body.Tokens != nil && *body.Tokens > 0 {
		profile.Tokens = *body.Tokens
	}

	received := time.Now()
	live := s.inflight.Add(1)
	for {
		peak := s.peak.Load()
		if live <= peak || s.peak.CompareAndSwap(peak, live) {
			break
		}
	}
	defer s.inflight.Add(-1)

	if !wait(r, profile.TTFT) {
		s.aborted.Add(1)
		return
	}

	if !body.Stream {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set(HeaderReceived, strconv.FormatInt(received.UnixNano(), 10))
		if !wait(r, time.Duration(profile.Tokens)*profile.ITL) {
			s.aborted.Add(1)
			return
		}
		_, _ = fmt.Fprintf(w, `{"id":"transit","object":"chat.completion","model":%q,"transit_received_ns":%d,"choices":[{"index":0,"message":{"role":"assistant","content":%q},"finish_reason":"stop"}],"usage":{"prompt_tokens":16,"completion_tokens":%d,"total_tokens":%d}}`,
			body.Model, received.UnixNano(), content(profile.Tokens*profile.TokenBytes), profile.Tokens, profile.Tokens+16)
		s.served.Add(1)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set(HeaderReceived, strconv.FormatInt(received.UnixNano(), 10))
	w.WriteHeader(http.StatusOK)
	flusher := http.NewResponseController(w)
	token := content(profile.TokenBytes)

	for i := 0; i < profile.Tokens; i++ {
		if i > 0 && !wait(r, profile.ITL) {
			s.aborted.Add(1)
			return
		}
		if _, err := fmt.Fprintf(w, "data: {\"id\":\"transit\",\"object\":\"chat.completion.chunk\",\"model\":%q,\"transit_received_ns\":%d,\"transit_write_ns\":%d,\"choices\":[{\"index\":0,\"delta\":{\"content\":%q}}]}\n\n", body.Model, received.UnixNano(), time.Now().UnixNano(), token); err != nil {
			s.aborted.Add(1)
			return
		}
		if err := flusher.Flush(); err != nil {
			s.aborted.Add(1)
			return
		}
	}

	_, _ = fmt.Fprintf(w, "data: {\"id\":\"transit\",\"object\":\"chat.completion.chunk\",\"model\":%q,\"choices\":[],\"usage\":{\"prompt_tokens\":16,\"completion_tokens\":%d,\"total_tokens\":%d}}\n\n",
		body.Model, profile.Tokens, profile.Tokens+16)
	_, _ = fmt.Fprint(w, "data: [DONE]\n\n")
	_ = flusher.Flush()
	s.served.Add(1)
}

// wait sleeps unless the caller has hung up, and reports whether the answer
// should carry on. A cancelled request must stop generating: a fake frontend
// that finished anyway would hide every abort the transport produced.
func wait(r *http.Request, d time.Duration) bool {
	if d <= 0 {
		return r.Context().Err() == nil
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-r.Context().Done():
		return false
	case <-timer.C:
		return true
	}
}

func content(n int) string {
	if n <= 0 {
		return ""
	}
	out := make([]byte, n)
	for i := range out {
		out[i] = 'x'
	}
	return string(out)
}

// ParseProfile reads a profile from three strings, for a command line.
func ParseProfile(ttft, itl, tokens string) (Profile, error) {
	var p Profile
	if ttft != "" {
		d, err := time.ParseDuration(ttft)
		if err != nil {
			return p, fmt.Errorf("ttft: %w", err)
		}
		p.TTFT = d
	}
	if itl != "" {
		d, err := time.ParseDuration(itl)
		if err != nil {
			return p, fmt.Errorf("itl: %w", err)
		}
		p.ITL = d
	}
	if tokens != "" {
		n, err := strconv.Atoi(tokens)
		if err != nil {
			return p, fmt.Errorf("tokens: %w", err)
		}
		p.Tokens = n
	}
	return p.withDefaults(), nil
}
