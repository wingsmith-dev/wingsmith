package plane

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// MediaJob is one async media generation as the plane holds it (docs/decisions/0041).
type MediaJob struct {
	ID              string             `json:"id"`
	Tenant          string             `json:"tenant"`
	Project         string             `json:"project"`
	Endpoint        string             `json:"endpoint"`
	Kind            string             `json:"kind"`
	Path            string             `json:"path"`
	Model           string             `json:"model"`
	Status          string             `json:"status"`
	Progress        int                `json:"progress"`
	Attempts        int                `json:"attempts"`
	CancelRequested bool               `json:"cancel_requested"`
	CreatedAt       time.Time          `json:"created_at"`
	StartedAt       *time.Time         `json:"started_at"`
	CompletedAt     *time.Time         `json:"completed_at"`
	ExpiresAt       *time.Time         `json:"expires_at"`
	ObjectKey       string             `json:"object_key"`
	ContentType     string             `json:"content_type"`
	SizeBytes       int64              `json:"size_bytes"`
	UpstreamSeconds float64            `json:"upstream_seconds"`
	Units           map[string]float64 `json:"units"`
	Error           *JobError          `json:"error"`
	// Request is the body to replay upstream, present only on a claim.
	Request json.RawMessage `json:"request,omitempty"`
}

// JobError is why a job failed.
type JobError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// MediaJobCreate is a submission on a caller's behalf.
type MediaJobCreate struct {
	Tenant         string          `json:"tenant"`
	Endpoint       string          `json:"endpoint"`
	Kind           string          `json:"kind"`
	Path           string          `json:"path"`
	Model          string          `json:"model"`
	Request        json.RawMessage `json:"request"`
	IdempotencyKey string          `json:"idempotency_key,omitempty"`
}

// MediaJobComplete is what a worker reports when it is done with a job.
type MediaJobComplete struct {
	Worker string `json:"worker"`
	// Lease is the claim's Attempts, which fences this claim off from a later one.
	Lease           int                `json:"lease"`
	Status          string             `json:"status"`
	ObjectKey       string             `json:"object_key,omitempty"`
	ContentType     string             `json:"content_type,omitempty"`
	SizeBytes       int64              `json:"size_bytes,omitempty"`
	UpstreamSeconds float64            `json:"upstream_seconds,omitempty"`
	Units           map[string]float64 `json:"units,omitempty"`
	Error           *JobError          `json:"error,omitempty"`
}

// ErrJobNotFound is a job that does not exist or belongs to another tenant.
var ErrJobNotFound = errors.New("media job not found")

// ErrLeaseLost is a heartbeat or completion from a worker that no longer holds the job.
var ErrLeaseLost = errors.New("media job lease lost")

// ErrJobRefused is a submission the plane would not queue, such as an endpoint it does not know.
var ErrJobRefused = errors.New("media job refused")

func (c *Client) jobCall(ctx context.Context, method, path string, query url.Values, in, out any) (int, error) {
	var body io.Reader
	if in != nil {
		raw, err := json.Marshal(in)
		if err != nil {
			return 0, err
		}
		body = bytes.NewReader(raw)
	}
	target := c.baseURL + "/v1/gateway/media-jobs" + path
	if len(query) > 0 {
		target += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, target, body)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, err
	}
	defer func() { _ = resp.Body.Close() }()
	switch {
	case credentialRefused(resp.StatusCode):
		return resp.StatusCode, fmt.Errorf("plane media jobs: %w (status %d)", ErrCredential, resp.StatusCode)
	case resp.StatusCode == http.StatusNotFound:
		return resp.StatusCode, ErrJobNotFound
	case resp.StatusCode == http.StatusConflict:
		return resp.StatusCode, ErrLeaseLost
	case resp.StatusCode == http.StatusNoContent:
		return resp.StatusCode, nil
	case resp.StatusCode/100 == 4:
		return resp.StatusCode, fmt.Errorf("%w (status %d)", ErrJobRefused, resp.StatusCode)
	case resp.StatusCode/100 != 2:
		return resp.StatusCode, fmt.Errorf("plane media jobs: unexpected status %d", resp.StatusCode)
	}
	if out != nil {
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			return resp.StatusCode, fmt.Errorf("plane media jobs: decode: %w", err)
		}
	}
	return resp.StatusCode, nil
}

func tenantQuery(tenant string) url.Values {
	if tenant == "" {
		return nil
	}
	return url.Values{"tenant": {tenant}}
}

// CreateMediaJob queues a job, or returns the one already queued under the same idempotency key.
func (c *Client) CreateMediaJob(ctx context.Context, in MediaJobCreate) (MediaJob, error) {
	var job MediaJob
	_, err := c.jobCall(ctx, http.MethodPost, "", nil, in, &job)
	return job, err
}

// MediaJob reads a job; tenant empty reads any tenant's (the internal zone).
func (c *Client) MediaJob(ctx context.Context, id, tenant string) (MediaJob, error) {
	var job MediaJob
	_, err := c.jobCall(ctx, http.MethodGet, "/"+url.PathEscape(id), tenantQuery(tenant), nil, &job)
	return job, err
}

// CancelMediaJob cancels a job, or expires a finished one.
func (c *Client) CancelMediaJob(ctx context.Context, id, tenant string) (MediaJob, error) {
	var job MediaJob
	_, err := c.jobCall(ctx, http.MethodPost, "/"+url.PathEscape(id)+"/cancel", tenantQuery(tenant), struct{}{}, &job)
	return job, err
}

// ClaimMediaJob leases the oldest runnable job to worker; ok is false when there is none.
func (c *Client) ClaimMediaJob(ctx context.Context, worker string) (MediaJob, bool, error) {
	var job MediaJob
	status, err := c.jobCall(ctx, http.MethodPost, "/claim", nil, map[string]string{"worker": worker}, &job)
	if err != nil || status == http.StatusNoContent {
		return MediaJob{}, false, err
	}
	return job, true, nil
}

// HeartbeatMediaJob extends worker's lease, the claim's Attempts, and reports whether the caller
// asked to cancel.
func (c *Client) HeartbeatMediaJob(ctx context.Context, id, worker string, lease, progress int) (bool, error) {
	var out struct {
		CancelRequested bool `json:"cancel_requested"`
	}
	in := map[string]any{"worker": worker, "lease": lease, "progress": progress}
	_, err := c.jobCall(ctx, http.MethodPost, "/"+url.PathEscape(id)+"/heartbeat", nil, in, &out)
	return out.CancelRequested, err
}

// CompleteMediaJob records a job's outcome.
func (c *Client) CompleteMediaJob(ctx context.Context, id string, in MediaJobComplete) (MediaJob, error) {
	var job MediaJob
	_, err := c.jobCall(ctx, http.MethodPost, "/"+url.PathEscape(id)+"/complete", nil, in, &job)
	return job, err
}
