package plane

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestMediaJobCallsCarryTheTenantAndMapStatuses(t *testing.T) {
	var gotPath, gotTenant, gotAuth string
	status := http.StatusOK
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotTenant, gotAuth = r.URL.Path, r.URL.Query().Get("tenant"), r.Header.Get("Authorization")
		w.WriteHeader(status)
		if status == http.StatusOK {
			_ = json.NewEncoder(w).Encode(MediaJob{ID: "mjb_1", Status: "queued"})
		}
	}))
	defer srv.Close()
	c := New(srv.URL, "tok", 0)

	job, err := c.MediaJob(context.Background(), "mjb_1", "ten_a")
	if err != nil || job.ID != "mjb_1" || gotPath != "/v1/gateway/media-jobs/mjb_1" || gotTenant != "ten_a" || gotAuth != "Bearer tok" {
		t.Fatalf("read = %+v %v (%s %s %s)", job, err, gotPath, gotTenant, gotAuth)
	}
	status = http.StatusNotFound
	if _, err := c.CancelMediaJob(context.Background(), "mjb_1", "ten_a"); !errors.Is(err, ErrJobNotFound) || gotPath != "/v1/gateway/media-jobs/mjb_1/cancel" {
		t.Fatalf("cancel err = %v path %s", err, gotPath)
	}
	status = http.StatusConflict
	if _, err := c.HeartbeatMediaJob(context.Background(), "mjb_1", "gw", 1, 0); !errors.Is(err, ErrLeaseLost) {
		t.Fatalf("heartbeat err = %v", err)
	}
	status = http.StatusNoContent
	if _, ok, err := c.ClaimMediaJob(context.Background(), "gw"); ok || err != nil {
		t.Fatalf("empty claim = %v %v", ok, err)
	}
	status = http.StatusForbidden
	if _, err := c.CreateMediaJob(context.Background(), MediaJobCreate{}); !errors.Is(err, ErrCredential) {
		t.Fatalf("refused credential err = %v", err)
	}
}
