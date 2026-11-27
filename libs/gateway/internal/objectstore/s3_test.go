package objectstore

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// The presigned GET example from the AWS SigV4 documentation ("Authenticating Requests: Using
// Query Parameters"), which fixes every input and states the signature.
func TestPresignMatchesTheAWSExample(t *testing.T) {
	cfg := Config{Region: "us-east-1", AccessKeyID: "AKIAIOSFODNN7EXAMPLE", SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
	now := time.Date(2013, 5, 24, 0, 0, 0, 0, time.UTC)
	got := presign("https", "examplebucket.s3.amazonaws.com", "/test.txt", 24*time.Hour, now, cfg)
	want := "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
	if !strings.HasSuffix(got, want) {
		t.Fatalf("presign = %s, want suffix %s", got, want)
	}
}

func TestPresignIsPathStyleAndExpiresInTheTTL(t *testing.T) {
	s, err := New(Config{Endpoint: "http://minio.local:9000", Bucket: "media", AccessKeyID: "AK", SecretAccessKey: "SK"})
	if err != nil {
		t.Fatal(err)
	}
	s.now = func() time.Time { return time.Date(2026, 11, 27, 12, 0, 0, 0, time.UTC) }
	got := s.Presign("media/search/mjb_1", time.Hour)
	if !strings.HasPrefix(got, "http://minio.local:9000/media/media/search/mjb_1?") {
		t.Fatalf("presign = %s", got)
	}
	for _, part := range []string{"X-Amz-Expires=3600", "X-Amz-Date=20261127T120000Z", "X-Amz-Credential=AK%2F20261127%2Fus-east-1%2Fs3%2Faws4_request"} {
		if !strings.Contains(got, part) {
			t.Errorf("presign %s lacks %s", got, part)
		}
	}
}

func TestPutSendsASignedBody(t *testing.T) {
	var gotPath, gotAuth, gotType, gotBody string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		gotPath, gotAuth, gotType, gotBody = r.URL.Path, r.Header.Get("Authorization"), r.Header.Get("Content-Type"), string(b)
	}))
	defer srv.Close()
	s, err := New(Config{Endpoint: srv.URL, Bucket: "media", AccessKeyID: "AK", SecretAccessKey: "SK"})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Put(context.Background(), "media/p/j", strings.NewReader("video"), 5, "video/mp4"); err != nil {
		t.Fatal(err)
	}
	if gotPath != "/media/media/p/j" || gotType != "video/mp4" || gotBody != "video" {
		t.Fatalf("PUT %s %s %q", gotPath, gotType, gotBody)
	}
	if !strings.HasPrefix(gotAuth, "AWS4-HMAC-SHA256 Credential=AK/") || !strings.Contains(gotAuth, "SignedHeaders=host;x-amz-content-sha256;x-amz-date") {
		t.Fatalf("Authorization = %s", gotAuth)
	}
}

func TestPutReportsARefusal(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusForbidden) }))
	defer srv.Close()
	s, _ := New(Config{Endpoint: srv.URL, Bucket: "media"})
	if err := s.Put(context.Background(), "k", strings.NewReader(""), 0, ""); err == nil {
		t.Fatal("a 403 must be an error")
	}
}

func TestNewRefusesABadEndpoint(t *testing.T) {
	if _, err := New(Config{Endpoint: "minio:9000", Bucket: "b"}); err == nil {
		t.Fatal("a scheme-less endpoint must be refused")
	}
	if _, err := New(Config{Endpoint: "http://minio:9000"}); err == nil {
		t.Fatal("a missing bucket must be refused")
	}
}
