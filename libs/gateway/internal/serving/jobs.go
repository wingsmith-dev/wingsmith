package serving

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/metrics"
	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/plane"
)

// Async media jobs (docs/decisions/0041). The hop that receives a generation submits it to the
// plane and answers at once; a worker loop in some replica (jobworker.go) claims it, runs it
// through the internal-zone pipeline and writes the output to object storage. Nothing about a job
// is held in this process between requests.

// DefaultSignedURLTTL is how long a `/content` redirect stays valid.
const DefaultSignedURLTTL = time.Hour

// JobQueue is the plane's media job surface, as plane.Client implements it.
type JobQueue interface {
	CreateMediaJob(ctx context.Context, in plane.MediaJobCreate) (plane.MediaJob, error)
	MediaJob(ctx context.Context, id, tenant string) (plane.MediaJob, error)
	CancelMediaJob(ctx context.Context, id, tenant string) (plane.MediaJob, error)
	ClaimMediaJob(ctx context.Context, worker string) (plane.MediaJob, bool, error)
	HeartbeatMediaJob(ctx context.Context, id, worker string, lease, progress int) (bool, error)
	CompleteMediaJob(ctx context.Context, id string, in plane.MediaJobComplete) (plane.MediaJob, error)
}

// ObjectStore holds job outputs, as objectstore.Store implements it.
type ObjectStore interface {
	Put(ctx context.Context, key string, body io.Reader, size int64, contentType string) error
	Presign(key string, ttl time.Duration) string
}

// Jobs turns async media on. Nil keeps video synchronous and its read paths answered 404.
type Jobs struct {
	Queue JobQueue
	Store ObjectStore
	// URLTTL is how long a signed content URL is valid. Zero takes DefaultSignedURLTTL.
	URLTTL time.Duration
}

func (j *Jobs) ttl() time.Duration {
	if j.URLTTL > 0 {
		return j.URLTTL
	}
	return DefaultSignedURLTTL
}

// ObjectKey is where a job's output lives: media/{project}/{job}.
func ObjectKey(job plane.MediaJob) string {
	return "media/" + job.Project + "/" + job.ID
}

// jobRun is carried by a worker's request through the pipeline: what the submit reserved, and
// what the run spent, for the worker to report.
type jobRun struct {
	reserved map[string]float64
	units    map[string]float64
	upstream time.Duration
}

type jobRunKey struct{}

func withJobRun(ctx context.Context, run *jobRun) context.Context {
	return context.WithValue(ctx, jobRunKey{}, run)
}

func jobRunFrom(ctx context.Context) *jobRun {
	run, _ := ctx.Value(jobRunKey{}).(*jobRun)
	return run
}

// reservedUnits is what a submission charges up front and keeps: the seconds a video request
// asks for, or a speech request's characters. Recomputed by the worker from the same body, so the
// reservation needs no storage.
func reservedUnits(kind string, body []byte) map[string]float64 {
	facts := mediaFactsFromRequest(kind, body, false)
	switch {
	case kind == mediaVideo && facts.haveSeconds:
		return map[string]float64{"video_seconds": facts.seconds}
	case kind == mediaSpeech:
		return map[string]float64{"speech_characters": float64(facts.characters)}
	}
	return nil
}

// wantsAsync is whether a media request asks to become a job: always for `POST /v1/videos`,
// otherwise `Prefer: respond-async` or `"background": true`.
func wantsAsync(r *http.Request, body []byte) bool {
	if r.URL.Path == "/v1/videos" {
		return true
	}
	for _, v := range r.Header.Values("Prefer") {
		for _, part := range strings.Split(v, ",") {
			if strings.EqualFold(strings.TrimSpace(part), "respond-async") {
				return true
			}
		}
	}
	var envelope struct {
		Background bool `json:"background"`
	}
	_ = json.Unmarshal(body, &envelope)
	return envelope.Background
}

// withoutBackground drops `background` so an engine that does not know it never sees it.
func withoutBackground(body []byte) []byte {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		return body
	}
	if _, ok := fields["background"]; !ok {
		return body
	}
	delete(fields, "background")
	out, err := json.Marshal(fields)
	if err != nil {
		return body
	}
	return out
}

// stageAsyncIntent marks a media request that will be submitted as a job, ahead of the quota
// stage, which reserves for it instead of settling.
func stageAsyncIntent(jobs *Jobs) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if jobs == nil || jobRunFrom(r.Context()) != nil || r.Method != http.MethodPost {
			return r, true
		}
		kind := mediaKindFor(r.URL.Path, routeModality(req.route))
		if kind == "" || r.URL.Path == "/v1/chat/completions" || !wantsAsync(r, req.body) {
			return r, true
		}
		if req.isMultipart {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusBadRequest, "async_unsupported",
				"A multipart request cannot run as a background job. Send it synchronously.")
			return r, false
		}
		req.asyncKind = kind
		return r, true
	}
}

// stageSubmitJob queues a marked request with the plane and answers with the job. On a worker's
// own request it instead records what the run spent, for the worker to report.
func stageSubmitJob(jobs *Jobs) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if run := jobRunFrom(r.Context()); run != nil {
			if obs := observationFrom(r.Context()); obs != nil {
				req.release = append(req.release, func() {
					obs.mu.Lock()
					defer obs.mu.Unlock()
					if obs.media != nil {
						run.units = obs.media.units()
						run.upstream = obs.media.upstream
					}
				})
			}
			return r, true
		}
		if req.asyncKind == "" {
			return r, true
		}
		job, err := jobs.Queue.CreateMediaJob(r.Context(), plane.MediaJobCreate{
			Tenant:         req.route.TenantID,
			Endpoint:       req.route.ID,
			Kind:           req.asyncKind,
			Path:           r.URL.Path,
			Model:          req.envelope.Model,
			Request:        withoutBackground(req.body),
			IdempotencyKey: r.Header.Get("Idempotency-Key"),
		})
		if err != nil {
			slog.Warn("gateway: media job submit failed", "err", err, "endpoint", req.route.ID)
			MarkOutcome(r, metrics.OutcomeShed)
			writeError(w, http.StatusServiceUnavailable, "job_queue_unavailable",
				"The job could not be queued. Retry shortly.")
			return r, false
		}
		req.asyncSubmitted = true
		MarkOutcome(r, metrics.OutcomeOK)
		if req.asyncKind == mediaVideo && r.URL.Path == "/v1/videos" {
			writeJSON(w, http.StatusOK, videoObject(job))
			return r, false
		}
		w.Header().Set("Location", "/v1/media/jobs/"+job.ID)
		writeJSON(w, http.StatusAccepted, jobObject(job))
		return r, false
	}
}

// stageJobReads answers the read half of async media: GET a job, GET its content, DELETE it.
// Without Jobs it is stageVideoRetrieval, which says video is synchronous.
func stageJobReads(jobs *Jobs) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		if jobs == nil {
			return stageVideoRetrieval(w, r, req)
		}
		video := strings.HasPrefix(r.URL.Path, "/v1/videos/")
		generic := strings.HasPrefix(r.URL.Path, "/v1/media/jobs/")
		if r.Method == http.MethodPost || (!video && !generic) {
			if r.Method != http.MethodPost && (r.URL.Path == "/v1/videos" || r.URL.Path == "/v1/media/jobs") {
				markEndpoint(r, metrics.EndpointUnknown, "")
				MarkOutcome(r, metrics.OutcomeClientError)
				writeError(w, http.StatusNotFound, "unsupported_path", "Listing jobs is not supported. Read one by id.")
				return r, false
			}
			return r, true
		}
		prefix := "/v1/media/jobs/"
		if video {
			prefix = "/v1/videos/"
		}
		rest := strings.TrimPrefix(r.URL.Path, prefix)
		id, sub, _ := strings.Cut(rest, "/")
		markEndpoint(r, metrics.EndpointUnknown, "")
		if id == "" || (sub != "" && sub != "content") || (sub == "content" && r.Method != http.MethodGet) ||
			(r.Method != http.MethodGet && r.Method != http.MethodDelete) {
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusNotFound, "unsupported_path", r.Method+" "+r.URL.Path+" is not answered here.")
			return r, false
		}

		// The public zone sees only its own tenant's jobs; the internal zone, which resolves no
		// tenant, sees any.
		tenant := req.tenant.ID
		var job plane.MediaJob
		var err error
		if r.Method == http.MethodDelete {
			job, err = jobs.Queue.CancelMediaJob(r.Context(), id, tenant)
		} else {
			job, err = jobs.Queue.MediaJob(r.Context(), id, tenant)
		}
		if err == nil && video && job.Kind != mediaVideo {
			err = plane.ErrJobNotFound
		}
		switch {
		case errors.Is(err, plane.ErrJobNotFound):
			MarkOutcome(r, metrics.OutcomeClientError)
			writeError(w, http.StatusNotFound, "not_found", "No job "+id+".")
			return r, false
		case err != nil:
			slog.Warn("gateway: media job read failed", "err", err, "job", id)
			MarkOutcome(r, metrics.OutcomeShed)
			writeError(w, http.StatusServiceUnavailable, "job_queue_unavailable", "The job could not be read. Retry shortly.")
			return r, false
		}

		MarkOutcome(r, metrics.OutcomeOK)
		switch {
		case sub == "content":
			if job.Status != "succeeded" || job.ObjectKey == "" {
				MarkOutcome(r, metrics.OutcomeClientError)
				writeError(w, http.StatusConflict, "not_ready", "Job "+id+" is "+job.Status+"; it has no content.")
				return r, false
			}
			w.Header().Set("Cache-Control", "no-store")
			http.Redirect(w, r, jobs.Store.Presign(job.ObjectKey, jobs.ttl()), http.StatusFound)
		case r.Method == http.MethodDelete && video:
			writeJSON(w, http.StatusOK, map[string]any{"id": job.ID, "object": "video.deleted", "deleted": true})
		case video:
			writeJSON(w, http.StatusOK, videoObject(job))
		default:
			writeJSON(w, http.StatusOK, jobObject(job))
		}
		return r, false
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func unix(t *time.Time) any {
	if t == nil {
		return nil
	}
	return t.Unix()
}

// videoObject is a job in OpenAI's Videos API shape. A cancelled job reads as failed with code
// `cancelled`, since that API has no cancelled status.
func videoObject(job plane.MediaJob) map[string]any {
	status := map[string]string{"queued": "queued", "running": "in_progress", "succeeded": "completed"}[job.Status]
	jobErr := job.Error
	if status == "" {
		status = "failed"
		if job.Status == "cancelled" {
			jobErr = &plane.JobError{Code: "cancelled", Message: "The job was cancelled."}
		}
	}
	var asked struct {
		Seconds json.RawMessage `json:"seconds"`
		Size    string          `json:"size"`
	}
	_ = json.Unmarshal(job.Request, &asked)
	seconds := strings.Trim(string(asked.Seconds), `"`)
	out := map[string]any{
		"id":           job.ID,
		"object":       "video",
		"model":        job.Model,
		"status":       status,
		"progress":     job.Progress,
		"created_at":   job.CreatedAt.Unix(),
		"completed_at": unix(job.CompletedAt),
		"expires_at":   unix(job.ExpiresAt),
		"error":        nil,
	}
	if seconds != "" && seconds != "null" {
		out["seconds"] = seconds
	}
	if asked.Size != "" {
		out["size"] = asked.Size
	}
	if jobErr != nil {
		out["error"] = jobErr
	}
	return out
}

// jobObject is a generic media job.
func jobObject(job plane.MediaJob) map[string]any {
	out := map[string]any{
		"id":           job.ID,
		"object":       "media.job",
		"model":        job.Model,
		"kind":         job.Kind,
		"status":       job.Status,
		"progress":     job.Progress,
		"created_at":   job.CreatedAt.Unix(),
		"completed_at": unix(job.CompletedAt),
		"expires_at":   unix(job.ExpiresAt),
		"error":        job.Error,
	}
	if job.Status == "succeeded" {
		out["content_url"] = "/v1/media/jobs/" + job.ID + "/content"
		out["content_type"] = job.ContentType
	}
	return out
}
