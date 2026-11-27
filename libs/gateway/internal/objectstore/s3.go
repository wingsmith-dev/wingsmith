// Package objectstore writes media job outputs to S3-compatible storage and presigns reads of
// them (docs/decisions/0041). Path-style SigV4 without an SDK: one PUT and one presigned GET are
// all the gateway needs, and MinIO, R2, GCS interop and AWS all accept both.
package objectstore

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	algorithm       = "AWS4-HMAC-SHA256"
	unsignedPayload = "UNSIGNED-PAYLOAD"
	stampFormat     = "20060102T150405Z"
)

// Config names a bucket and the credentials for it.
type Config struct {
	Endpoint        string
	Bucket          string
	Region          string
	AccessKeyID     string
	SecretAccessKey string
}

// FromEnv reads WINGSMITH_MEDIA_S3_{ENDPOINT,BUCKET,REGION,ACCESS_KEY_ID,SECRET_ACCESS_KEY}. It
// returns nil, nil when no endpoint or bucket is set: async media is then off.
func FromEnv() (*Store, error) {
	cfg := Config{
		Endpoint:        os.Getenv("WINGSMITH_MEDIA_S3_ENDPOINT"),
		Bucket:          os.Getenv("WINGSMITH_MEDIA_S3_BUCKET"),
		Region:          os.Getenv("WINGSMITH_MEDIA_S3_REGION"),
		AccessKeyID:     os.Getenv("WINGSMITH_MEDIA_S3_ACCESS_KEY_ID"),
		SecretAccessKey: os.Getenv("WINGSMITH_MEDIA_S3_SECRET_ACCESS_KEY"),
	}
	if cfg.Endpoint == "" || cfg.Bucket == "" {
		return nil, nil
	}
	return New(cfg)
}

// Store is one bucket.
type Store struct {
	base *url.URL
	cfg  Config
	HTTP *http.Client
	now  func() time.Time
}

// New builds a store. The endpoint must be an absolute http(s) URL.
func New(cfg Config) (*Store, error) {
	base, err := url.Parse(strings.TrimRight(cfg.Endpoint, "/"))
	if err != nil || (base.Scheme != "http" && base.Scheme != "https") || base.Host == "" {
		return nil, fmt.Errorf("objectstore: endpoint %q is not an http(s) URL", cfg.Endpoint)
	}
	if cfg.Bucket == "" {
		return nil, errors.New("objectstore: bucket is required")
	}
	if cfg.Region == "" {
		cfg.Region = "us-east-1"
	}
	return &Store{base: base, cfg: cfg, HTTP: &http.Client{Timeout: 30 * time.Minute}, now: time.Now}, nil
}

func (s *Store) path(key string) string {
	return s.base.Path + "/" + escapePath(s.cfg.Bucket) + "/" + escapePath(key)
}

// Put uploads size bytes from body under key.
func (s *Store) Put(ctx context.Context, key string, body io.Reader, size int64, contentType string) error {
	path := s.path(key)
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, s.base.Scheme+"://"+s.base.Host+path, body)
	if err != nil {
		return err
	}
	req.ContentLength = size
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	stamp := s.now().UTC().Format(stampFormat)
	headers := map[string]string{
		"host":                 s.base.Host,
		"x-amz-content-sha256": unsignedPayload,
		"x-amz-date":           stamp,
	}
	signed, signature := sign(http.MethodPut, path, "", headers, unsignedPayload, stamp, s.cfg)
	req.Header.Set("X-Amz-Content-Sha256", unsignedPayload)
	req.Header.Set("X-Amz-Date", stamp)
	req.Header.Set("Authorization", fmt.Sprintf("%s Credential=%s/%s, SignedHeaders=%s, Signature=%s",
		algorithm, s.cfg.AccessKeyID, scope(stamp, s.cfg.Region), signed, signature))
	resp, err := s.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("objectstore: PUT %s: %s", key, resp.Status)
	}
	return nil
}

// Presign returns a GET URL for key that is valid for ttl.
func (s *Store) Presign(key string, ttl time.Duration) string {
	return presign(s.base.Scheme, s.base.Host, s.path(key), ttl, s.now().UTC(), s.cfg)
}

func presign(scheme, host, path string, ttl time.Duration, now time.Time, cfg Config) string {
	stamp := now.Format(stampFormat)
	query := url.Values{}
	query.Set("X-Amz-Algorithm", algorithm)
	query.Set("X-Amz-Credential", cfg.AccessKeyID+"/"+scope(stamp, cfg.Region))
	query.Set("X-Amz-Date", stamp)
	query.Set("X-Amz-Expires", strconv.Itoa(int(ttl.Seconds())))
	query.Set("X-Amz-SignedHeaders", "host")
	canonicalQuery := canonicalQueryString(query)
	_, signature := sign(http.MethodGet, path, canonicalQuery, map[string]string{"host": host}, unsignedPayload, stamp, cfg)
	return scheme + "://" + host + path + "?" + canonicalQuery + "&X-Amz-Signature=" + signature
}

func scope(stamp, region string) string {
	return stamp[:8] + "/" + region + "/s3/aws4_request"
}

// sign returns the signed header list and the signature of one request.
func sign(method, path, query string, headers map[string]string, payload, stamp string, cfg Config) (string, string) {
	names := make([]string, 0, len(headers))
	for k := range headers {
		names = append(names, k)
	}
	sort.Strings(names)
	var canonicalHeaders strings.Builder
	for _, k := range names {
		canonicalHeaders.WriteString(k + ":" + strings.TrimSpace(headers[k]) + "\n")
	}
	signed := strings.Join(names, ";")
	canonical := strings.Join([]string{method, path, query, canonicalHeaders.String(), signed, payload}, "\n")
	digest := sha256.Sum256([]byte(canonical))
	toSign := strings.Join([]string{algorithm, stamp, scope(stamp, cfg.Region), hex.EncodeToString(digest[:])}, "\n")
	key := hmacSHA256([]byte("AWS4"+cfg.SecretAccessKey), stamp[:8])
	key = hmacSHA256(key, cfg.Region)
	key = hmacSHA256(key, "s3")
	key = hmacSHA256(key, "aws4_request")
	return signed, hex.EncodeToString(hmacSHA256(key, toSign))
}

func hmacSHA256(key []byte, msg string) []byte {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(msg))
	return m.Sum(nil)
}

// escapePath is SigV4's URI encoding of a path: every byte but unreserved ones and `/`.
func escapePath(p string) string {
	var b strings.Builder
	for i := 0; i < len(p); i++ {
		c := p[i]
		if c == '/' || unreserved(c) {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

func unreserved(c byte) bool {
	return 'A' <= c && c <= 'Z' || 'a' <= c && c <= 'z' || '0' <= c && c <= '9' || c == '-' || c == '_' || c == '.' || c == '~'
}

func canonicalQueryString(v url.Values) string {
	keys := make([]string, 0, len(v))
	for k := range v {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, escapeQuery(k)+"="+escapeQuery(v.Get(k)))
	}
	return strings.Join(parts, "&")
}

func escapeQuery(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if unreserved(s[i]) {
			b.WriteByte(s[i])
		} else {
			fmt.Fprintf(&b, "%%%02X", s[i])
		}
	}
	return b.String()
}
