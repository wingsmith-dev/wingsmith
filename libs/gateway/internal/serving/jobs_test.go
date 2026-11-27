package serving

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/objectstore"
	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/plane"
	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/routing"
)

// fakeQueue is the plane's media job surface in memory: tenant-scoped reads, idempotency keys and
// one lease per job.
type fakeQueue struct {
	mu        sync.Mutex
	jobs      map[string]*plane.MediaJob
	order     []string
	keys      map[string]string
	completed map[string]plane.MediaJobComplete
	cancel    bool
}

func newFakeQueue() *fakeQueue {
	return &fakeQueue{jobs: map[string]*plane.MediaJob{}, keys: map[string]string{}, completed: map[string]plane.MediaJobComplete{}}
}

func (q *fakeQueue) CreateMediaJob(_ context.Context, in plane.MediaJobCreate) (plane.MediaJob, error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if in.IdempotencyKey != "" {
		if id, ok := q.keys[in.Tenant+"/"+in.IdempotencyKey]; ok {
			return *q.jobs[id], nil
		}
	}
	id := "mjb_" + string(rune('a'+len(q.order)))
	job := &plane.MediaJob{ID: id, Tenant: in.Tenant, Project: "search", Endpoint: in.Endpoint, Kind: in.Kind,
		Path: in.Path, Model: in.Model, Status: "queued", Request: in.Request, CreatedAt: time.Unix(1_800_000_000, 0)}
	q.jobs[id] = job
	q.order = append(q.order, id)
	if in.IdempotencyKey != "" {
		q.keys[in.Tenant+"/"+in.IdempotencyKey] = id
	}
	return *job, nil
}

func (q *fakeQueue) find(id, tenant string) (*plane.MediaJob, error) {
	job, ok := q.jobs[id]
	if !ok || (tenant != "" && job.Tenant != tenant) {
		return nil, plane.ErrJobNotFound
	}
	return job, nil
}

func (q *fakeQueue) MediaJob(_ context.Context, id, tenant string) (plane.MediaJob, error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	job, err := q.find(id, tenant)
	if err != nil {
		return plane.MediaJob{}, err
	}
	return *job, nil
}

func (q *fakeQueue) CancelMediaJob(_ context.Context, id, tenant string) (plane.MediaJob, error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	job, err := q.find(id, tenant)
	if err != nil {
		return plane.MediaJob{}, err
	}
	switch job.Status {
	case "queued":
		job.Status = "cancelled"
	case "running":
		job.CancelRequested = true
	}
	return *job, nil
}

func (q *fakeQueue) ClaimMediaJob(_ context.Context, worker string) (plane.MediaJob, bool, error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for _, id := range q.order {
		if job := q.jobs[id]; job.Status == "queued" {
			job.Status, job.Attempts = "running", job.Attempts+1
			return *job, true, nil
		}
	}
	return plane.MediaJob{}, false, nil
}

func (q *fakeQueue) HeartbeatMediaJob(_ context.Context, id, worker string, _ int) (bool, error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.cancel || q.jobs[id].CancelRequested, nil
}

func (q *fakeQueue) CompleteMediaJob(_ context.Context, id string, in plane.MediaJobComplete) (plane.MediaJob, error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	job := q.jobs[id]
	job.Status, job.ObjectKey, job.ContentType, job.Units = in.Status, in.ObjectKey, in.ContentType, in.Units
	job.Error = in.Error
	q.completed[id] = in
	return *job, nil
}

// fakeS3 is an in-process S3: PUT stores, GET serves only a presigned URL.
type fakeS3 struct {
	mu      sync.Mutex
	objects map[string][]byte
	types   map[string]string
	server  *httptest.Server
}

func newFakeS3(t *testing.T) *fakeS3 {
	s := &fakeS3{objects: map[string][]byte{}, types: map[string]string{}}
	s.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		defer s.mu.Unlock()
		switch r.Method {
		case http.MethodPut:
			if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256 ") {
				w.WriteHeader(http.StatusForbidden)
				return
			}
			b, _ := io.ReadAll(r.Body)
			s.objects[r.URL.Path], s.types[r.URL.Path] = b, r.Header.Get("Content-Type")
		case http.MethodGet:
			if r.URL.Query().Get("X-Amz-Signature") == "" || r.URL.Query().Get("X-Amz-Expires") != "3600" {
				w.WriteHeader(http.StatusForbidden)
				return
			}
			b, ok := s.objects[r.URL.Path]
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			_, _ = w.Write(b)
		}
	}))
	t.Cleanup(s.server.Close)
	return s
}

type jobRig struct {
	queue  *fakeQueue
	s3     *fakeS3
	public http.Handler
	worker *JobWorker
	seen   chan string
}

func newJobRig(t *testing.T, modality string, upstream http.HandlerFunc, limits *plane.Limits) *jobRig {
	t.Helper()
	seen := make(chan string, 8)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		seen <- string(b)
		upstream(w, r)
	}))
	t.Cleanup(server.Close)
	route := modalityRoute(t, "gen", server.URL, modality)
	route.ID, route.TenantID, route.Visibility, route.Limits = "end_gen", "ten_globex", "public", limits
	resolver := stubResolver{routes: map[string]routing.Route{"gen": route}}

	s3 := newFakeS3(t)
	store, err := objectstore.New(objectstore.Config{Endpoint: s3.server.URL, Bucket: "media", AccessKeyID: "AK", SecretAccessKey: "SK"})
	if err != nil {
		t.Fatal(err)
	}
	queue := newFakeQueue()
	jobs := &Jobs{Queue: queue, Store: store}
	quotas := NewUnitQuotas(1)
	other := plane.GatewayTenant{ID: "ten_other", Slug: "other", Enabled: true, Ingress: "public", Auth: "api_key",
		Host: "other.wingsmith.dev", KeyHashes: []string{keyHash("other-key")}}
	public := New(Config{Router: resolver, Tenants: tenantsFor(t, globex(), other), Zone: ZonePublic, Jobs: jobs, UnitQuotas: quotas})
	internal := New(Config{Router: resolver, Zone: ZoneInternal, Jobs: jobs, UnitQuotas: quotas})
	return &jobRig{
		queue: queue, s3: s3, public: public, seen: seen,
		worker: &JobWorker{Queue: queue, Store: store, Handler: internal, ID: "gw-1", Heartbeat: 5 * time.Millisecond},
	}
}

func (g *jobRig) call(t *testing.T, method, path, body string, headers ...string) *httptest.ResponseRecorder {
	t.Helper()
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	r.Host = "globex.wingsmith.dev"
	r.Header.Set("Authorization", "Bearer secret-key")
	for i := 0; i+1 < len(headers); i += 2 {
		r.Header.Set(headers[i], headers[i+1])
	}
	rec := httptest.NewRecorder()
	g.public.ServeHTTP(rec, r)
	return rec
}

func decode(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode %q: %v", rec.Body.String(), err)
	}
	return out
}

func videoUpstream(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"data":[{"b64_json":"` + base64.StdEncoding.EncodeToString([]byte("MP4DATA")) + `"}],"inference_time_s":3}`))
}

func TestAVideoRunsAsAJobEndToEnd(t *testing.T) {
	g := newJobRig(t, "video", videoUpstream, nil)

	rec := g.call(t, http.MethodPost, "/v1/videos", `{"model":"gen","prompt":"a cat","seconds":"4","size":"832x480"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("submit = %d %s", rec.Code, rec.Body.String())
	}
	video := decode(t, rec)
	id := video["id"].(string)
	if video["object"] != "video" || video["status"] != "queued" || video["seconds"] != "4" || video["size"] != "832x480" {
		t.Fatalf("submitted = %v", video)
	}
	select {
	case body := <-g.seen:
		t.Fatalf("the submit reached the engine: %s", body)
	default:
	}

	ran, err := g.worker.RunOnce(context.Background())
	if !ran || err != nil {
		t.Fatalf("RunOnce = %v %v", ran, err)
	}
	done := g.queue.completed[id]
	if done.Status != "succeeded" || done.ObjectKey != "media/search/"+id || done.ContentType != "video/mp4" || done.UpstreamSeconds <= 0 {
		t.Fatalf("completed = %+v", done)
	}
	if done.Units["video_seconds"] != 4 {
		t.Fatalf("units = %v, want video_seconds 4", done.Units)
	}
	if got := string(g.s3.objects["/media/media/search/"+id]); got != "MP4DATA" {
		t.Fatalf("stored %q", got)
	}

	if got := decode(t, g.call(t, http.MethodGet, "/v1/videos/"+id, "")); got["status"] != "completed" {
		t.Fatalf("read = %v", got)
	}
	content := g.call(t, http.MethodGet, "/v1/videos/"+id+"/content", "")
	if content.Code != http.StatusFound {
		t.Fatalf("content = %d %s", content.Code, content.Body.String())
	}
	location := content.Header().Get("Location")
	if !strings.HasPrefix(location, g.s3.server.URL+"/media/media/search/"+id+"?") {
		t.Fatalf("Location = %s", location)
	}
	resp, err := http.Get(location)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if string(b) != "MP4DATA" {
		t.Fatalf("signed URL served %d %q", resp.StatusCode, b)
	}
}

func TestAnotherTenantCannotReadOrDeleteAJob(t *testing.T) {
	g := newJobRig(t, "video", videoUpstream, nil)
	id := decode(t, g.call(t, http.MethodPost, "/v1/videos", `{"model":"gen","prompt":"x"}`))["id"].(string)
	for _, method := range []string{http.MethodGet, http.MethodDelete} {
		r := httptest.NewRequest(method, "/v1/videos/"+id, nil)
		r.Host = "other.wingsmith.dev"
		r.Header.Set("Authorization", "Bearer other-key")
		rec := httptest.NewRecorder()
		g.public.ServeHTTP(rec, r)
		if rec.Code != http.StatusNotFound {
			t.Fatalf("%s by another tenant = %d, want 404", method, rec.Code)
		}
	}
	if g.queue.jobs[id].Status != "queued" {
		t.Fatalf("another tenant's DELETE changed the job: %v", g.queue.jobs[id].Status)
	}
}

func TestDeleteCancelsAQueuedVideo(t *testing.T) {
	g := newJobRig(t, "video", videoUpstream, nil)
	id := decode(t, g.call(t, http.MethodPost, "/v1/videos", `{"model":"gen","prompt":"x"}`))["id"].(string)
	got := decode(t, g.call(t, http.MethodDelete, "/v1/videos/"+id, ""))
	if got["object"] != "video.deleted" || got["deleted"] != true {
		t.Fatalf("delete = %v", got)
	}
	read := decode(t, g.call(t, http.MethodGet, "/v1/videos/"+id, ""))
	if read["status"] != "failed" || read["error"].(map[string]any)["code"] != "cancelled" {
		t.Fatalf("read after delete = %v", read)
	}
	if c := g.call(t, http.MethodGet, "/v1/videos/"+id+"/content", ""); c.Code != http.StatusConflict {
		t.Fatalf("content of a cancelled job = %d, want 409", c.Code)
	}
}

func TestAnIdempotencyKeyReturnsTheSameVideo(t *testing.T) {
	g := newJobRig(t, "video", videoUpstream, nil)
	a := decode(t, g.call(t, http.MethodPost, "/v1/videos", `{"model":"gen","prompt":"x"}`, "Idempotency-Key", "k1"))
	b := decode(t, g.call(t, http.MethodPost, "/v1/videos", `{"model":"gen","prompt":"x"}`, "Idempotency-Key", "k1"))
	if a["id"] != b["id"] || len(g.queue.order) != 1 {
		t.Fatalf("ids %v %v, jobs %d", a["id"], b["id"], len(g.queue.order))
	}
}

func TestAnImageRunsAsyncOnRequestAndTheEngineNeverSeesBackground(t *testing.T) {
	image := func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[{"b64_json":"` + base64.StdEncoding.EncodeToString([]byte("PNG")) + `"}]}`))
	}
	g := newJobRig(t, "image", image, nil)

	sync := g.call(t, http.MethodPost, "/v1/images/generations", `{"model":"gen","prompt":"x"}`)
	if sync.Code != http.StatusOK || !strings.Contains(sync.Body.String(), "b64_json") {
		t.Fatalf("a plain image request must stay synchronous: %d %s", sync.Code, sync.Body.String())
	}
	<-g.seen

	for _, tc := range []struct{ body, prefer string }{
		{`{"model":"gen","prompt":"x","background":true}`, ""},
		{`{"model":"gen","prompt":"x"}`, "respond-async, wait=0"},
	} {
		rec := g.call(t, http.MethodPost, "/v1/images/generations", tc.body, "Prefer", tc.prefer)
		if rec.Code != http.StatusAccepted {
			t.Fatalf("async submit = %d %s", rec.Code, rec.Body.String())
		}
		job := decode(t, rec)
		if job["object"] != "media.job" || rec.Header().Get("Location") != "/v1/media/jobs/"+job["id"].(string) {
			t.Fatalf("job = %v, Location %s", job, rec.Header().Get("Location"))
		}
		if _, err := g.worker.RunOnce(context.Background()); err != nil {
			t.Fatal(err)
		}
		if body := <-g.seen; strings.Contains(body, "background") {
			t.Fatalf("engine saw %s", body)
		}
		read := decode(t, g.call(t, http.MethodGet, "/v1/media/jobs/"+job["id"].(string), ""))
		if read["status"] != "succeeded" || read["content_type"] != "image/png" {
			t.Fatalf("read = %v", read)
		}
		// A generic job is not readable as a video.
		if c := g.call(t, http.MethodGet, "/v1/videos/"+job["id"].(string), ""); c.Code != http.StatusNotFound {
			t.Fatalf("image job read as video = %d", c.Code)
		}
	}
}

func TestAnUpstreamFailureFailsTheJob(t *testing.T) {
	g := newJobRig(t, "video", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":{"message":"num_frames too large"}}`))
	}, nil)
	id := decode(t, g.call(t, http.MethodPost, "/v1/videos", `{"model":"gen","prompt":"x"}`))["id"].(string)
	if _, err := g.worker.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	done := g.queue.completed[id]
	if done.Status != "failed" || done.Error == nil || done.Error.Message != "num_frames too large" {
		t.Fatalf("completed = %+v", done)
	}
	if len(g.s3.objects) != 0 {
		t.Fatal("a failed job stored an object")
	}
}

func TestACancelRequestedWhileRunningStopsTheJob(t *testing.T) {
	g := newJobRig(t, "video", func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}, nil)
	id := decode(t, g.call(t, http.MethodPost, "/v1/videos", `{"model":"gen","prompt":"x"}`))["id"].(string)
	g.queue.cancel = true
	if _, err := g.worker.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := g.queue.completed[id].Status; got != "cancelled" {
		t.Fatalf("status = %s, want cancelled", got)
	}
}

func TestAVideoReservesItsSecondsAtSubmit(t *testing.T) {
	limits := &plane.Limits{UnitLimits: map[string]int64{"video_seconds": 10}, Enforced: true}
	g := newJobRig(t, "video", videoUpstream, limits)
	if rec := g.call(t, http.MethodPost, "/v1/videos", `{"model":"gen","prompt":"x","seconds":"8"}`); rec.Code != http.StatusOK {
		t.Fatalf("first submit = %d", rec.Code)
	}
	// Running it settles the difference only (8 generated, 8 reserved), so the budget is unchanged.
	if _, err := g.worker.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if rec := g.call(t, http.MethodPost, "/v1/videos", `{"model":"gen","prompt":"x","seconds":"8"}`); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("second submit = %d, want 429: the first one's reservation still holds", rec.Code)
	}
}

func TestWithoutJobsVideoReadsStayUnsupported(t *testing.T) {
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{}}})
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/videos/abc", nil))
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "synchronous") {
		t.Fatalf("status = %d %s", rec.Code, rec.Body.String())
	}
}
