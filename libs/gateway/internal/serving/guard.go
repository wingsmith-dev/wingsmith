package serving

import (
	"crypto/subtle"
	"net/http"
	"strings"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
)

// Guard decides whether a caller may use this gateway at all.
//
// This is not user authentication and must not grow into it. The gateway has
// one kind of client — the system in front of it, which has already decided who
// its user is — so the question here is only whether this hop is being called
// by that system.
type Guard interface {
	Allow(r *http.Request) bool
}

// TokenGuard accepts any of a small set of shared bearer tokens.
//
// A set rather than one string, and the reason is rotation. With a single token
// the changeover is instantaneous on both sides at once: the moment this gateway
// reads a new value every caller still holding the old one is rejected, and
// there is no ordering of the two deploys that avoids it. Accepting the old and
// the new together makes it three independent steps — add, move the callers,
// drop — none of which is a coordinated flip.
//
// It stays a set of shared secrets and does not become a credential store. These
// say nothing about who is calling; they say the caller is the system that fronts
// this hop. The moment one of them identifies a tenant, this has grown into the
// user authentication the Guard doc says it must not.
type TokenGuard struct {
	tokens [][]byte
}

// NewTokenGuard reads a comma-separated set, ignoring blanks and surrounding
// space so a trailing comma or a wrapped Secret value is not a token of its own.
//
// Returns nil when nothing usable is left, so a gateway with nothing configured
// is explicitly open rather than accidentally accepting "" — or, now, accepting
// a value that was all separators.
func NewTokenGuard(token string) *TokenGuard {
	var tokens [][]byte
	for _, candidate := range strings.Split(token, ",") {
		if trimmed := strings.TrimSpace(candidate); trimmed != "" {
			tokens = append(tokens, []byte(trimmed))
		}
	}
	if len(tokens) == 0 {
		return nil
	}
	return &TokenGuard{tokens: tokens}
}

// Accepted reports how many tokens this guard will admit.
//
// For the log line at startup, so that "both tokens are loaded" is checkable
// before the callers are moved to the new one rather than after. The count is
// safe to emit; the tokens are not.
func (g *TokenGuard) Accepted() int {
	return len(g.tokens)
}

// Allow reports whether the request carries one of the tokens, compared in
// constant time so a wrong guess leaks nothing about how wrong it was.
//
// Every token is compared even after one matches. Returning early would make the
// response time report which token was presented, and during a rotation that is
// the difference between the old secret and the new one.
func (g *TokenGuard) Allow(r *http.Request) bool {
	header := r.Header.Get("Authorization")
	presented, ok := strings.CutPrefix(header, "Bearer ")
	if !ok {
		return false
	}
	matched := 0
	for _, token := range g.tokens {
		matched |= subtle.ConstantTimeCompare([]byte(presented), token)
	}
	return matched == 1
}

func withGuard(guard Guard, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !guard.Allow(r) {
			// The caller's, not ours: a rejected credential is outside every
			// availability objective's ratio rather than inside its numerator.
			MarkOutcome(r, metrics.OutcomeAuthRejected)
			writeError(w, http.StatusUnauthorized, "unauthorized", "This gateway does not accept unidentified callers.")
			return
		}
		next.ServeHTTP(w, r)
	})
}
