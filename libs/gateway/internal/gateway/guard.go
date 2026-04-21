package gateway

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

// TokenGuard accepts one shared bearer token.
type TokenGuard struct {
	token string
}

// NewTokenGuard returns nil for an empty token, so a gateway with nothing
// configured is explicitly open rather than accidentally accepting "".
func NewTokenGuard(token string) *TokenGuard {
	if token == "" {
		return nil
	}
	return &TokenGuard{token: token}
}

// Allow reports whether the request carries the token, compared in constant
// time so a wrong guess leaks nothing about how wrong it was.
func (g *TokenGuard) Allow(r *http.Request) bool {
	header := r.Header.Get("Authorization")
	presented, ok := strings.CutPrefix(header, "Bearer ")
	if !ok {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(presented), []byte(g.token)) == 1
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
