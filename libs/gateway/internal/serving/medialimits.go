package serving

import (
	"context"
	"net/http"
	"time"
)

// pathLimit is what one media path may cost this hop: how large its body may be and how long the
// upstream may take to finish answering it. Zero is "the listener's own bound".
type pathLimit struct {
	maxBytes int64
	timeout  time.Duration
}

// mediaPathLimits are the per-modality bounds (P4.2). The body caps follow what each API carries:
// a prompt or a speech `input` is a small JSON body, an image edit or a transcription carries an
// upload (25 MiB is OpenAI's own audio file limit). The timeouts are whole-request deadlines on a
// synchronous answer: an image is about two minutes on any GPU we place one on, a transcription of
// a long file or a video takes longer. Text paths have neither: a generation legitimately runs for
// minutes and is bounded by the write idle timeout instead.
var mediaPathLimits = map[string]pathLimit{
	"/v1/images/generations":   {maxBytes: 1 << 20, timeout: 120 * time.Second},
	"/v1/images/edits":         {maxBytes: 50 << 20, timeout: 120 * time.Second},
	"/v1/audio/speech":         {maxBytes: 1 << 20, timeout: 120 * time.Second},
	"/v1/audio/transcriptions": {maxBytes: 25<<20 + 1<<20, timeout: 300 * time.Second},
	"/v1/audio/translations":   {maxBytes: 25<<20 + 1<<20, timeout: 300 * time.Second},
	"/v1/videos":               {maxBytes: 50 << 20, timeout: 600 * time.Second},
	"/v1/videos/stream":        {maxBytes: 50 << 20, timeout: 600 * time.Second},
}

// bodyCap is the body limit for a path: the path's own cap when it is tighter than the
// listener's, the listener's otherwise.
func bodyCap(path string, listener int64) int64 {
	if listener <= 0 {
		listener = DefaultMaxBodyBytes
	}
	if limit, ok := mediaPathLimits[path]; ok && limit.maxBytes > 0 && limit.maxBytes < listener {
		return limit.maxBytes
	}
	return listener
}

// withUpstreamDeadline bounds a media request's time inside a cluster. The cancel is the caller's
// to run once the response has been written.
func withUpstreamDeadline(r *http.Request) (*http.Request, context.CancelFunc) {
	limit, ok := mediaPathLimits[r.URL.Path]
	if !ok || limit.timeout <= 0 {
		return r, func() {}
	}
	ctx, cancel := context.WithTimeout(r.Context(), limit.timeout)
	return r.WithContext(ctx), cancel
}
