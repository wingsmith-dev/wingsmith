package serving

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/plane"
)

// JobWorker claims media jobs from the plane and runs them (docs/decisions/0041).
//
// It runs each job as a request through Handler, which is an internal-zone handler on this
// replica, so routing, modality, metering and the tunnel are the code a synchronous request
// takes. The response is spooled to a temporary file and uploaded to the store; only the key
// goes back to the plane.
type JobWorker struct {
	Queue   JobQueue
	Store   ObjectStore
	Handler http.Handler
	// ID names this worker on its leases. Unique per replica.
	ID string
	// Concurrency is how many jobs this replica runs at once. Less than one is one.
	Concurrency int
	// Poll is how long an idle worker waits before claiming again. Zero is two seconds.
	Poll time.Duration
	// Heartbeat is how often a running job's lease is extended. Zero is fifteen seconds, a
	// quarter of the plane's default lease.
	Heartbeat time.Duration
	// Timeout bounds one job. Zero is thirty minutes.
	Timeout time.Duration
}

// Run claims and runs jobs until ctx ends.
func (wk *JobWorker) Run(ctx context.Context) {
	var wg sync.WaitGroup
	for range max(wk.Concurrency, 1) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for ctx.Err() == nil {
				ran, err := wk.RunOnce(ctx)
				if err != nil {
					slog.Warn("gateway: media job claim failed", "err", err)
				}
				if !ran {
					select {
					case <-ctx.Done():
					case <-time.After(durationOr(wk.Poll, 2*time.Second)):
					}
				}
			}
		}()
	}
	wg.Wait()
}

func durationOr(d, def time.Duration) time.Duration {
	if d > 0 {
		return d
	}
	return def
}

// RunOnce claims one job and runs it to the end, reporting whether there was one.
func (wk *JobWorker) RunOnce(ctx context.Context) (bool, error) {
	job, ok, err := wk.Queue.ClaimMediaJob(ctx, wk.ID)
	if err != nil || !ok {
		return false, err
	}
	wk.run(ctx, job)
	return true, nil
}

func (wk *JobWorker) run(parent context.Context, job plane.MediaJob) {
	ctx, cancel := context.WithTimeout(parent, durationOr(wk.Timeout, 30*time.Minute))
	defer cancel()

	var mu sync.Mutex
	var lost, cancelled bool
	beatDone := make(chan struct{})
	go func() {
		defer close(beatDone)
		tick := time.NewTicker(durationOr(wk.Heartbeat, 15*time.Second))
		defer tick.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-tick.C:
			}
			stop, err := wk.Queue.HeartbeatMediaJob(ctx, job.ID, wk.ID, 0)
			if errors.Is(err, plane.ErrLeaseLost) || stop {
				mu.Lock()
				lost, cancelled = lost || err != nil, cancelled || stop
				mu.Unlock()
				cancel()
				return
			}
		}
	}()

	outcome := wk.execute(ctx, job)
	cancel()
	<-beatDone
	mu.Lock()
	wasLost, wasCancelled := lost, cancelled
	mu.Unlock()
	if parent.Err() != nil {
		// Shutting down: leave the lease to lapse so another replica runs the job again.
		slog.Info("gateway: media job abandoned on shutdown", "job", job.ID, "worker", wk.ID)
		return
	}
	if wasLost {
		// Another worker holds it now, or will; nothing here may report.
		slog.Warn("gateway: media job lease lost", "job", job.ID, "worker", wk.ID)
		return
	}
	if wasCancelled {
		outcome = plane.MediaJobComplete{Status: "cancelled", UpstreamSeconds: outcome.UpstreamSeconds}
	}
	outcome.Worker = wk.ID
	// The report outlives the job's own deadline: the work is done and should be recorded.
	report, done := context.WithTimeout(context.WithoutCancel(parent), 30*time.Second)
	defer done()
	if _, err := wk.Queue.CompleteMediaJob(report, job.ID, outcome); err != nil {
		slog.Warn("gateway: media job complete failed", "job", job.ID, "err", err)
		return
	}
	// Metering: one line per finished job, with the upstream's own timing.
	slog.Info("media_job.finished",
		"job", job.ID, "tenant", job.Tenant, "project", job.Project, "endpoint", job.Endpoint,
		"kind", job.Kind, "status", outcome.Status, "upstream_seconds", outcome.UpstreamSeconds,
		"units", outcome.Units, "bytes", outcome.SizeBytes, "attempt", job.Attempts)
}

func failed(code, message string, upstream float64) plane.MediaJobComplete {
	return plane.MediaJobComplete{Status: "failed", Error: &plane.JobError{Code: code, Message: message}, UpstreamSeconds: upstream}
}

// execute runs the job upstream and stores its output, returning what to report.
func (wk *JobWorker) execute(ctx context.Context, job plane.MediaJob) plane.MediaJobComplete {
	body := withoutBackground(job.Request)
	run := &jobRun{reserved: reservedUnits(job.Kind, body)}
	req, err := http.NewRequestWithContext(withJobRun(ctx, run), http.MethodPost, job.Path, bytes.NewReader(body))
	if err != nil {
		return failed("invalid_job", err.Error(), 0)
	}
	req.Header.Set("Content-Type", "application/json")
	req.ContentLength = int64(len(body))

	spool, err := os.CreateTemp("", "wingsmith-job-*")
	if err != nil {
		return failed("spool_failed", err.Error(), 0)
	}
	defer func() { _ = spool.Close(); _ = os.Remove(spool.Name()) }()
	rec := &spoolWriter{header: http.Header{}, file: spool}

	start := time.Now()
	wk.Handler.ServeHTTP(rec, req)
	upstream := run.upstream
	if upstream <= 0 {
		upstream = time.Since(start)
	}
	seconds := upstream.Seconds()
	if rec.err != nil {
		return failed("spool_failed", rec.err.Error(), seconds)
	}
	if ctx.Err() != nil {
		return failed("timeout", "the job ran past its deadline", seconds)
	}
	if rec.status/100 != 2 {
		return failed("upstream_error", jobErrorMessage(spool, rec.status), seconds)
	}

	content, contentType, size, err := outputOf(spool, rec.header.Get("Content-Type"), job.Kind)
	if err != nil {
		return failed("invalid_output", err.Error(), seconds)
	}
	key := ObjectKey(job)
	if err := wk.Store.Put(ctx, key, content, size, contentType); err != nil {
		return failed("store_failed", err.Error(), seconds)
	}
	return plane.MediaJobComplete{
		Status:          "succeeded",
		ObjectKey:       key,
		ContentType:     contentType,
		SizeBytes:       size,
		UpstreamSeconds: seconds,
		Units:           run.units,
	}
}

// jobErrorMessage is the error an engine answered a job with, bounded.
func jobErrorMessage(spool *os.File, status int) string {
	_, _ = spool.Seek(0, io.SeekStart)
	raw, _ := io.ReadAll(io.LimitReader(spool, 4096))
	var envelope struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &envelope) == nil && envelope.Error.Message != "" {
		return envelope.Error.Message
	}
	return http.StatusText(status)
}

// outputContentType is what a decoded `b64_json` output is, by kind.
var outputContentType = map[string]string{
	mediaVideo: "video/mp4",
	mediaImage: "image/png",
}

// outputOf is the object to store: the decoded `data[0].b64_json` of a JSON media response, or
// the response as it came (an audio stream, or JSON carrying only URLs).
func outputOf(spool *os.File, contentType, kind string) (io.Reader, string, int64, error) {
	info, err := spool.Stat()
	if err != nil {
		return nil, "", 0, err
	}
	if _, err := spool.Seek(0, io.SeekStart); err != nil {
		return nil, "", 0, err
	}
	if !strings.HasPrefix(contentType, "application/json") || outputContentType[kind] == "" {
		return spool, contentType, info.Size(), nil
	}
	var parsed struct {
		Data []struct {
			B64JSON string `json:"b64_json"`
		} `json:"data"`
	}
	if err := json.NewDecoder(spool).Decode(&parsed); err != nil {
		return nil, "", 0, err
	}
	if len(parsed.Data) == 0 || parsed.Data[0].B64JSON == "" {
		_, _ = spool.Seek(0, io.SeekStart)
		return spool, contentType, info.Size(), nil
	}
	decoded, err := base64.StdEncoding.DecodeString(parsed.Data[0].B64JSON)
	if err != nil {
		return nil, "", 0, err
	}
	return bytes.NewReader(decoded), outputContentType[kind], int64(len(decoded)), nil
}

// spoolWriter is the ResponseWriter a job runs against: headers in memory, body on disk.
type spoolWriter struct {
	header http.Header
	status int
	file   *os.File
	err    error
}

func (s *spoolWriter) Header() http.Header { return s.header }

func (s *spoolWriter) WriteHeader(code int) {
	if s.status == 0 {
		s.status = code
	}
}

func (s *spoolWriter) Write(b []byte) (int, error) {
	if s.status == 0 {
		s.status = http.StatusOK
	}
	n, err := s.file.Write(b)
	if err != nil && s.err == nil {
		s.err = err
	}
	return n, err
}

// Flush is a no-op, so a streaming proxy writes straight through.
func (s *spoolWriter) Flush() {}
