package serving

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
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
	// beat extends the lease and reports whether the job may go on. A plane that does not answer
	// is not a lost lease: the next beat tries again, and the lease outlives a few misses.
	beat := func(ctx context.Context) bool {
		stop, err := wk.Queue.HeartbeatMediaJob(ctx, job.ID, wk.ID, job.Attempts, 0)
		if !errors.Is(err, plane.ErrLeaseLost) && !stop {
			return true
		}
		mu.Lock()
		lost, cancelled = lost || err != nil, cancelled || stop
		mu.Unlock()
		return false
	}
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
			if !beat(ctx) {
				cancel()
				return
			}
		}
	}()

	outcome := wk.execute(ctx, job, beat)
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
	outcome.Worker, outcome.Lease = wk.ID, job.Attempts
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

// execute runs the job upstream and stores its output, returning what to report. beat confirms the
// lease before the upload, so a worker that lost the job leaves no object behind.
func (wk *JobWorker) execute(ctx context.Context, job plane.MediaJob, beat func(context.Context) bool) plane.MediaJobComplete {
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
	if content != spool {
		defer func() { _ = content.Close(); _ = os.Remove(content.Name()) }()
	}
	if !beat(ctx) {
		// run reports nothing for a lost lease and cancelled for a cancel.
		return failed("lease_lost", "the job's lease was lost before its output was stored", seconds)
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
// the response as it came (an audio stream, or JSON carrying only URLs). The base64 is decoded as
// a stream into a second spool file, so a large output never sits in memory.
func outputOf(spool *os.File, contentType, kind string) (*os.File, string, int64, error) {
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
	dec := json.NewDecoder(spool)
	found, err := seekFirstB64(dec)
	if err != nil {
		return nil, "", 0, err
	}
	if !found {
		if _, err := spool.Seek(0, io.SeekStart); err != nil {
			return nil, "", 0, err
		}
		return spool, contentType, info.Size(), nil
	}
	out, err := os.CreateTemp("", "wingsmith-job-out-*")
	if err != nil {
		return nil, "", 0, err
	}
	fail := func(err error) (*os.File, string, int64, error) {
		_ = out.Close()
		_ = os.Remove(out.Name())
		return nil, "", 0, err
	}
	value := &b64String{r: bufio.NewReader(io.MultiReader(dec.Buffered(), spool))}
	if err := value.open(); err != nil {
		return fail(err)
	}
	size, err := io.Copy(out, base64.NewDecoder(base64.StdEncoding, value))
	if err != nil {
		return fail(err)
	}
	if size == 0 {
		return fail(errors.New("empty b64_json"))
	}
	if _, err := out.Seek(0, io.SeekStart); err != nil {
		return fail(err)
	}
	return out, outputContentType[kind], size, nil
}

// seekFirstB64 walks dec to just past the key of `data[0].b64_json`, skipping every other value,
// and reports whether it is there. The value itself is left unread.
func seekFirstB64(dec *json.Decoder) (bool, error) {
	if err := expectDelim(dec, '{'); err != nil {
		return false, err
	}
	for dec.More() {
		key, err := dec.Token()
		if err != nil {
			return false, err
		}
		if key != "data" {
			if err := skipJSON(dec); err != nil {
				return false, err
			}
			continue
		}
		tok, err := dec.Token()
		if err != nil {
			return false, err
		}
		if tok != json.Delim('[') || !dec.More() {
			return false, nil
		}
		if err := expectDelim(dec, '{'); err != nil {
			return false, nil
		}
		for dec.More() {
			field, err := dec.Token()
			if err != nil {
				return false, err
			}
			if field == "b64_json" {
				return true, nil
			}
			if err := skipJSON(dec); err != nil {
				return false, err
			}
		}
		return false, nil
	}
	return false, nil
}

func expectDelim(dec *json.Decoder, want json.Delim) error {
	tok, err := dec.Token()
	if err != nil {
		return err
	}
	if tok != want {
		return fmt.Errorf("expected %q", want)
	}
	return nil
}

func skipJSON(dec *json.Decoder) error {
	var skip json.RawMessage
	return dec.Decode(&skip)
}

// b64String reads the contents of a JSON string value from r, which is positioned before the
// `:` that precedes it, unescaping only what can appear in base64 (`\/`, and line breaks).
type b64String struct {
	r    *bufio.Reader
	done bool
}

func (s *b64String) open() error {
	for {
		c, err := s.r.ReadByte()
		if err != nil {
			return err
		}
		switch c {
		case ' ', '\t', '\n', '\r', ':':
		case '"':
			return nil
		default:
			return errors.New("b64_json is not a string")
		}
	}
}

func (s *b64String) Read(p []byte) (int, error) {
	n := 0
	for n < len(p) && !s.done {
		c, err := s.r.ReadByte()
		if err != nil {
			if err == io.EOF {
				err = io.ErrUnexpectedEOF
			}
			return n, err
		}
		switch c {
		case '"':
			s.done = true
			continue
		case '\\':
			e, err := s.r.ReadByte()
			if err != nil {
				return n, io.ErrUnexpectedEOF
			}
			switch e {
			case '/':
				c = '/'
			case 'n', 'r':
				continue
			default:
				return n, fmt.Errorf("unexpected escape \\%c in b64_json", e)
			}
		}
		p[n] = c
		n++
	}
	if n == 0 && s.done {
		return 0, io.EOF
	}
	return n, nil
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
