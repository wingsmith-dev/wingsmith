package dynamo

import (
	"context"
	"net/http"
	"strconv"
	"time"
)

// What became of a request that reached the agent. Closed set: these reach a
// metric label.
const (
	// OutcomeForwarded is a request the frontend answered, with any status.
	OutcomeForwarded = "forwarded"
	// OutcomeUnsupportedPath is a request for a path the agent does not forward.
	OutcomeUnsupportedPath = "unsupported_path"
	// OutcomeNoFrontend is a cluster with no frontend address yet.
	OutcomeNoFrontend = "no_frontend"
	// OutcomeDeploymentNotReady is a named deployment whose frontend is not up.
	OutcomeDeploymentNotReady = "deployment_frontend_not_ready"
	// OutcomeFrontendError is a frontend address that could not be resolved.
	OutcomeFrontendError = "frontend_error"
	// OutcomeFrontendUnavailable is a frontend that did not answer.
	OutcomeFrontendUnavailable = "frontend_unavailable"
	// OutcomeCanceled is a request whose caller went away before an answer.
	OutcomeCanceled = "canceled"
	// OutcomeAborted is a response cut after it started, by either side.
	OutcomeAborted = "aborted"
)

// Observer is told about every request the handler serves. The agent's
// metrics implement it.
type Observer interface {
	RequestStarted()
	// RequestFinished reports the outcome, the status class written ("2xx" to
	// "5xx", empty when nothing was written) and the time from arrival to the
	// last byte.
	RequestFinished(outcome, code string, elapsed time.Duration)
}

// Option adjusts NewHandler.
type Option func(*handlerConfig)

type handlerConfig struct {
	observer Observer
}

// WithObserver reports every request to o.
func WithObserver(o Observer) Option {
	return func(c *handlerConfig) { c.observer = o }
}

type outcomeKey struct{}

// setOutcome records how a request ended, for the observer. A request served
// without an observer carries nowhere to record it, and this does nothing.
func setOutcome(r *http.Request, outcome string) {
	if slot, ok := r.Context().Value(outcomeKey{}).(*string); ok {
		*slot = outcome
	}
}

func observed(observer Observer, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		outcome := OutcomeForwarded
		r = r.WithContext(context.WithValue(r.Context(), outcomeKey{}, &outcome))
		status := &statusWriter{ResponseWriter: w}
		observer.RequestStarted()
		defer func() {
			// The proxy aborts a response it could not finish copying by
			// panicking with http.ErrAbortHandler. Count it, then let it go on.
			if recovered := recover(); recovered != nil {
				outcome = OutcomeAborted
				defer panic(recovered)
			}
			code := ""
			if status.status != 0 {
				code = strconv.Itoa(status.status/100) + "xx"
			}
			observer.RequestFinished(outcome, code, time.Since(start))
		}()
		next.ServeHTTP(status, r)
	})
}

// statusWriter remembers the status written. Unwrap keeps Flush reachable
// through http.ResponseController, which is how the proxy flushes every SSE
// event.
type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(status int) {
	if w.status == 0 {
		w.status = status
	}
	w.ResponseWriter.WriteHeader(status)
}

func (w *statusWriter) Write(b []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	return w.ResponseWriter.Write(b)
}

func (w *statusWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }
