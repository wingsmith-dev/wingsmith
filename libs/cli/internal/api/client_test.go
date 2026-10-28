package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/errs"
)

// serveJSON stands a plane up and points a client at it. The handler sees the real request the
// CLI would have sent, which is the half these tests are about.
func serveJSON(t *testing.T, handler http.HandlerFunc) (*Client, *httptest.Server) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	c, err := NewClient(srv.URL, "ws_pat_secret", "prj_1", "1.2.3", 5*time.Second)
	if err != nil {
		t.Fatalf("client: %v", err)
	}
	return c, srv
}

func page(items ...any) string {
	body, _ := json.Marshal(map[string]any{"items": items, "count": len(items), "next_cursor": ""})
	return string(body)
}

// TestNewClientRefusesWithoutAServerAndSaysHowToFixIt. A fresh checkout has no server configured,
// and this is the first error most people will see.
func TestNewClientRefusesWithoutAServer(t *testing.T) {
	_, err := NewClient("", "", "", "1.0", time.Second)
	if err == nil {
		t.Fatal("a client was built with no server")
	}
	e := errs.As(err)
	if e.Code != errs.CodeUsage {
		t.Errorf("code = %v, want usage", e.Code)
	}
	if len(e.Remediation) == 0 {
		t.Error("the refusal offers no way forward")
	}
}

// TestNewClientTrimsATrailingSlash: `https://plane.example/` plus `/v1/clusters` would otherwise
// request `//v1/clusters`, which some proxies answer with a redirect and others with a 404.
func TestNewClientTrimsATrailingSlash(t *testing.T) {
	c, err := NewClient("https://plane.example/", "", "", "1.0", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if c.Server != "https://plane.example" {
		t.Fatalf("server = %q, want no trailing slash", c.Server)
	}
}

func TestALiveClientIsNotSimulated(t *testing.T) {
	c, _ := NewClient("https://plane.example", "", "", "1.0", time.Second)
	if c.Simulated() {
		t.Fatal("the live client claims to be the fake one")
	}
}

// TestEveryRequestCarriesTheCredential. No project header goes with it: the plane does not
// scope a request by one, and sending it would suggest that it does.
func TestEveryRequestCarriesTheCredential(t *testing.T) {
	var got *http.Request
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		got = r.Clone(r.Context())
		fmt.Fprint(w, page())
	})
	if _, err := c.Clusters(context.Background(), ListOpts{}); err != nil {
		t.Fatalf("list: %v", err)
	}
	if h := got.Header.Get("Authorization"); h != "Bearer ws_pat_secret" {
		t.Errorf("Authorization = %q", h)
	}
	if _, present := got.Header["X-Wingsmith-Project"]; present {
		t.Error("the configured project was sent as an X-Wingsmith-Project header")
	}
	if h := got.Header.Get("Accept"); h != "application/json" {
		t.Errorf("Accept = %q", h)
	}
	if h := got.Header.Get("User-Agent"); h != "wings/1.2.3" {
		t.Errorf("User-Agent = %q, want the CLI's version", h)
	}
	if got.URL.Path != "/v1/clusters" {
		t.Errorf("path = %q", got.URL.Path)
	}
}

// TestAnUnauthenticatedClientSendsNoAuthorizationHeader: an empty bearer is worse than none,
// because a proxy may accept the header and reject the value with a different status.
func TestAnUnauthenticatedClientSendsNoAuthorizationHeader(t *testing.T) {
	var got *http.Request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Clone(r.Context())
		fmt.Fprint(w, page())
	}))
	defer srv.Close()
	c, _ := NewClient(srv.URL, "", "", "1.0", time.Second)
	if _, err := c.Clusters(context.Background(), ListOpts{}); err != nil {
		t.Fatal(err)
	}
	if _, present := got.Header["Authorization"]; present {
		t.Error("an empty token was sent as a bearer credential")
	}
}

// TestAListFetchesOnePageUnlessAskedForEverything. The server has no "give me everything" mode
// and adding one would be a request with no bound on its own cost, so following the cursor is the
// client's job, but only when the caller asked.
func TestAListFetchesOnePageUnlessAskedForEverything(t *testing.T) {
	var paths []string
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.RawQuery)
		fmt.Fprint(w, `{"items":[{"id":"clu_1"}],"count":1,"next_cursor":"c2"}`)
	})

	got, err := c.Clusters(context.Background(), ListOpts{})
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(paths) != 1 {
		t.Fatalf("made %d requests without --all, want 1", len(paths))
	}
	if got.NextCursor != "c2" {
		t.Errorf("the cursor was not handed back: %q", got.NextCursor)
	}
}

func TestListAllFollowsTheCursorToTheEnd(t *testing.T) {
	var cursors []string
	pages := []string{
		`{"items":[{"id":"clu_1"}],"count":1,"next_cursor":"c2"}`,
		`{"items":[{"id":"clu_2"}],"count":1,"next_cursor":"c3"}`,
		`{"items":[{"id":"clu_3"}],"count":1,"next_cursor":""}`,
	}
	i := 0
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		cursors = append(cursors, r.URL.Query().Get("cursor"))
		fmt.Fprint(w, pages[i])
		i++
	})

	got, err := c.Clusters(context.Background(), ListOpts{All: true})
	if err != nil {
		t.Fatalf("list all: %v", err)
	}
	if len(got.Items) != 3 {
		t.Fatalf("collected %d items across pages, want 3", len(got.Items))
	}
	if got.Count != 3 {
		t.Errorf("count = %d, want the total actually collected", got.Count)
	}
	if got.NextCursor != "" {
		t.Errorf("a fully drained listing still reports a cursor: %q", got.NextCursor)
	}
	want := []string{"", "c2", "c3"}
	for i, w := range want {
		if cursors[i] != w {
			t.Errorf("request %d sent cursor %q, want %q", i, cursors[i], w)
		}
	}
}

// TestListAllKeepsTheOriginalFiltersOnEveryPage. A second page fetched without the filter would
// silently widen the result set halfway through a listing.
func TestListAllKeepsTheFiltersOnEveryPage(t *testing.T) {
	var queries []string
	i := 0
	pages := []string{
		`{"items":[{"id":"clu_1"}],"count":1,"next_cursor":"c2"}`,
		`{"items":[{"id":"clu_2"}],"count":1,"next_cursor":""}`,
	}
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		queries = append(queries, r.URL.RawQuery)
		fmt.Fprint(w, pages[i])
		i++
	})

	if _, err := c.Clusters(context.Background(), ListOpts{All: true, Region: "us-east-1", Phase: "ready"}); err != nil {
		t.Fatal(err)
	}
	for i, q := range queries {
		if !strings.Contains(q, "region=us-east-1") || !strings.Contains(q, "phase=ready") {
			t.Errorf("page %d dropped its filters: %q", i, q)
		}
	}
}

// TestAnEmptyPageEndsTheWalk guards against a server that returns a cursor forever.
func TestAnEmptyPageEndsTheWalk(t *testing.T) {
	calls := 0
	c, _ := serveJSON(t, func(w http.ResponseWriter, _ *http.Request) {
		calls++
		if calls > 20 {
			t.Error("the cursor walk did not terminate")
			fmt.Fprint(w, `{"items":[],"count":0,"next_cursor":""}`)
			return
		}
		fmt.Fprint(w, `{"items":[],"count":0,"next_cursor":"always"}`)
	})
	if _, err := c.Clusters(context.Background(), ListOpts{All: true}); err != nil {
		t.Fatal(err)
	}
	if calls > 3 {
		t.Errorf("made %d requests against a server that never stops paginating", calls)
	}
}

// TestGetEscapesTheReference: a ref with a slash in it must not become a different path.
func TestGetEscapesTheReference(t *testing.T) {
	var path string
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.EscapedPath()
		fmt.Fprint(w, `{"id":"clu_1"}`)
	})
	if _, err := c.Cluster(context.Background(), "a/b"); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(path, "a/b") {
		t.Fatalf("the reference was not escaped: %q", path)
	}
}

// TestTheStatusCodeBecomesTheErrorCode. Scripts branch on the CLI's code, so the mapping from
// HTTP is part of the contract.
func TestTheStatusCodeBecomesTheErrorCode(t *testing.T) {
	cases := map[int]errs.Code{
		http.StatusBadRequest:          errs.CodeUsage,
		http.StatusUnprocessableEntity: errs.CodeUsage,
		http.StatusUnauthorized:        errs.CodeAuthRequired,
		http.StatusForbidden:           errs.CodeForbidden,
		http.StatusNotFound:            errs.CodeNotFound,
		http.StatusConflict:            errs.CodeConflict,
		http.StatusTooManyRequests:     errs.CodeRateLimited,
		http.StatusInternalServerError: errs.CodeServerError,
		http.StatusBadGateway:          errs.CodeServerError,
	}
	for status, want := range cases {
		c, _ := serveJSON(t, func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(status)
			fmt.Fprint(w, `{"message":"nope"}`)
		})
		_, err := c.Clusters(context.Background(), ListOpts{})
		if err == nil {
			t.Errorf("HTTP %d was accepted", status)
			continue
		}
		if got := errs.As(err).Code; got != want {
			t.Errorf("HTTP %d mapped to %v, want %v", status, got, want)
		}
	}
}

// TestThePlanesOwnCodeWinsOverTheStatus. The plane's `invalid` is a category error instead of a
// validation failure and answers 422, so it maps to INVALID_STATE and not USAGE.
func TestThePlanesOwnCodeWinsOverTheStatus(t *testing.T) {
	cases := []struct {
		status int
		wire   string
		want   errs.Code
	}{
		{http.StatusUnprocessableEntity, "invalid", errs.CodeInvalidState},
		{http.StatusBadRequest, "invalid_request", errs.CodeUsage},
		{http.StatusInternalServerError, "not_found", errs.CodeNotFound},
		{http.StatusOK + 400, "not_authorized", errs.CodeAuthRequired},
		{http.StatusConflict, "conflict", errs.CodeConflict},
		{http.StatusInternalServerError, "http_error", errs.CodeServerError},
		{http.StatusNotFound, "unrecognised", errs.CodeNotFound},
	}
	for _, tc := range cases {
		c, _ := serveJSON(t, func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(tc.status)
			fmt.Fprintf(w, `{"code":%q,"message":"nope"}`, tc.wire)
		})
		_, err := c.Clusters(context.Background(), ListOpts{})
		if got := errs.As(err).Code; got != tc.want {
			t.Errorf("%d/%s mapped to %v, want %v", tc.status, tc.wire, got, tc.want)
		}
	}
}

// TestAnErrorWithNoBodyStillSaysSomething: a bare 502 from a proxy has no JSON at all.
func TestAnErrorWithNoBodyStillSaysSomething(t *testing.T) {
	c, _ := serveJSON(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	})
	_, err := c.Clusters(context.Background(), ListOpts{})
	if err == nil {
		t.Fatal("a bodyless error was accepted")
	}
	if msg := err.Error(); msg == "" || msg == " " {
		t.Fatal("the error has no message at all")
	}
	if !strings.Contains(errs.As(err).Cause, "502") {
		t.Errorf("the cause does not name the status: %q", errs.As(err).Cause)
	}
}

// TestANonJSONErrorBodyIsPassedThrough: an HTML error page from a proxy is still information.
func TestANonJSONErrorBodyIsPassedThrough(t *testing.T) {
	c, _ := serveJSON(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		fmt.Fprint(w, "upstream connect error")
	})
	_, err := c.Clusters(context.Background(), ListOpts{})
	if !strings.Contains(err.Error(), "upstream connect error") {
		t.Fatalf("the proxy's own message was dropped: %q", err.Error())
	}
}

// TestDetailIsRenderedInAStableOrder: a map iterated at random would make the same failure print
// differently on every run, which is unusable in a test or a diff.
func TestDetailIsRenderedInAStableOrder(t *testing.T) {
	c, _ := serveJSON(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		fmt.Fprint(w, `{"code":"conflict","message":"nope","detail":{"zeta":1,"alpha":"a","mid":true}}`)
	})
	var first string
	for i := 0; i < 8; i++ {
		_, err := c.Clusters(context.Background(), ListOpts{})
		cause := errs.As(err).Cause
		if i == 0 {
			first = cause
			if cause != "alpha=a mid=true zeta=1" {
				t.Fatalf("detail = %q, want it sorted by key", cause)
			}
			continue
		}
		if cause != first {
			t.Fatalf("detail rendered differently across runs: %q then %q", first, cause)
		}
	}
}

// TestRateLimitingCarriesRetryAfter so the caller knows how long to wait instead of guessing.
func TestRateLimitingCarriesRetryAfter(t *testing.T) {
	c, _ := serveJSON(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Retry-After", "42")
		w.WriteHeader(http.StatusTooManyRequests)
		fmt.Fprint(w, `{"message":"slow down"}`)
	})
	_, err := c.Clusters(context.Background(), ListOpts{})
	e := errs.As(err)
	if !e.Retryable || e.RetryAfterS != 42 {
		t.Fatalf("retry = %v after %ds, want retryable after 42", e.Retryable, e.RetryAfterS)
	}
}

// TestAnUnparseableRetryAfterIsIgnoredRatherThanFatal: the header may be an HTTP date, which
// this client does not read, and that is not a reason to fail differently.
func TestAnUnparseableRetryAfterIsIgnored(t *testing.T) {
	c, _ := serveJSON(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Retry-After", "Wed, 21 Oct 2026 07:28:00 GMT")
		w.WriteHeader(http.StatusTooManyRequests)
		fmt.Fprint(w, `{"message":"slow down"}`)
	})
	_, err := c.Clusters(context.Background(), ListOpts{})
	e := errs.As(err)
	if e.Code != errs.CodeRateLimited {
		t.Fatalf("code = %v, want rate limited", e.Code)
	}
	if e.RetryAfterS != 0 {
		t.Errorf("an HTTP-date Retry-After was read as %d seconds", e.RetryAfterS)
	}
}

// TestAnUnreachablePlaneIsANetworkErrorWithAWayOut: the fake backend is the offline escape hatch
// and the error is where someone would find out it exists.
func TestAnUnreachablePlaneIsANetworkErrorWithAWayOut(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close()

	c, _ := NewClient(url, "", "", "1.0", time.Second)
	_, err := c.Clusters(context.Background(), ListOpts{})
	if err == nil {
		t.Fatal("a closed server answered")
	}
	e := errs.As(err)
	if e.Code != errs.CodeNetwork {
		t.Fatalf("code = %v, want network", e.Code)
	}
	var offered bool
	for _, a := range e.Remediation {
		if strings.Contains(a.Command, "--fake") {
			offered = true
		}
	}
	if !offered {
		t.Error("an unreachable plane does not mention the offline backend")
	}
}

// TestACancelledContextIsATimeoutNotANetworkFailure: the two mean different things to a caller
// deciding whether to retry.
func TestACancelledContextIsATimeout(t *testing.T) {
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	})
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	_, err := c.Clusters(ctx, ListOpts{})
	if err == nil {
		t.Fatal("a cancelled request succeeded")
	}
	if got := errs.As(err).Code; got != errs.CodeTimeout {
		t.Fatalf("code = %v, want timeout", got)
	}
}

// TestAResponseTheCLICannotDecodeIsAServerError, not a silent empty listing.
func TestAResponseTheCLICannotDecodeIsAServerError(t *testing.T) {
	c, _ := serveJSON(t, func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"items": "not a list"}`)
	})
	_, err := c.Clusters(context.Background(), ListOpts{})
	if err == nil {
		t.Fatal("an undecodable body was accepted as an empty listing")
	}
	if got := errs.As(err).Code; got != errs.CodeServerError {
		t.Fatalf("code = %v, want server error", got)
	}
}

// TestACreateSendsItsBodyAndIdempotencyKey. Operations are expensive and at-least-once delivery is
// the norm, so the key is what stops a retried drain from becoming two.
func TestACreateSendsItsBodyAndIdempotencyKey(t *testing.T) {
	var method, key, contentType string
	var body map[string]any
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		key = r.Header.Get("Idempotency-Key")
		contentType = r.Header.Get("Content-Type")
		_ = json.NewDecoder(r.Body).Decode(&body)
		fmt.Fprint(w, `{"id":"opr_1"}`)
	})

	_, err := c.CreateOperation(context.Background(),
		OperationRequest{Type: "node.drain", Target: "nod_1"}, "idem-123")
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if method != http.MethodPost {
		t.Errorf("method = %q, want POST", method)
	}
	if key != "idem-123" {
		t.Errorf("Idempotency-Key = %q", key)
	}
	if contentType != "application/json" {
		t.Errorf("Content-Type = %q", contentType)
	}
	if body["type"] != "node.drain" {
		t.Errorf("body = %v, want the operation it was given", body)
	}
}

func TestIdentityReportsWhetherThePlaneAnswers(t *testing.T) {
	c, _ := serveJSON(t, func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"status":true}`)
	})
	id, err := c.Identity(context.Background())
	if err != nil {
		t.Fatalf("identity: %v", err)
	}
	if id.Project != "prj_1" || !id.HasToken {
		t.Errorf("identity = %+v, want the configured context", id)
	}
	if !strings.HasPrefix(id.Server, "http") {
		t.Errorf("server = %q", id.Server)
	}
}

func TestParseDurationAcceptsBothSpellings(t *testing.T) {
	cases := map[string]time.Duration{
		"30":    30 * time.Second,
		"0":     0,
		"30s":   30 * time.Second,
		"5m":    5 * time.Minute,
		"1h":    time.Hour,
		" 90 ":  90 * time.Second,
		"1h30m": 90 * time.Minute,
	}
	for in, want := range cases {
		got, err := ParseDuration(in)
		if err != nil {
			t.Errorf("ParseDuration(%q): %v", in, err)
			continue
		}
		if got != want {
			t.Errorf("ParseDuration(%q) = %v, want %v", in, got, want)
		}
	}
	for _, bad := range []string{"", "soon", "5 minutes", "1x"} {
		if _, err := ParseDuration(bad); err == nil {
			t.Errorf("ParseDuration(%q) was accepted", bad)
		} else if errs.As(err).Code != errs.CodeUsage {
			t.Errorf("ParseDuration(%q) is not a usage error", bad)
		}
	}
}

func TestParseKV(t *testing.T) {
	got, err := ParseKV([]string{"a=1", "b=", "c=x=y"}, "label")
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	want := map[string]string{"a": "1", "b": "", "c": "x=y"}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s = %q, want %q", k, got[k], v)
		}
	}
	if got := mustNil(t); got != nil {
		t.Errorf("no pairs gave %v, want nil", got)
	}
	for _, bad := range []string{"nokey", "=value", " =value"} {
		if _, err := ParseKV([]string{bad}, "label"); err == nil {
			t.Errorf("ParseKV(%q) was accepted", bad)
		}
	}
}

func mustNil(t *testing.T) map[string]string {
	t.Helper()
	got, err := ParseKV(nil, "label")
	if err != nil {
		t.Fatal(err)
	}
	return got
}

func TestAgeRendersTheUnitAListingWants(t *testing.T) {
	now := time.Now()
	cases := []struct {
		since time.Duration
		want  string
	}{
		{2 * time.Second, "2s"},
		{90 * time.Second, "1m"},
		{45 * time.Minute, "45m"},
		{3 * time.Hour, "3h"},
		{47 * time.Hour, "47h"},
		{72 * time.Hour, "3d"},
		{40 * 24 * time.Hour, "40d"},
	}
	for _, c := range cases {
		if got := Age(now.Add(-c.since)); got != c.want {
			t.Errorf("Age(%v ago) = %q, want %q", c.since, got, c.want)
		}
	}
	if got := Age(time.Time{}); got != "-" {
		t.Errorf("Age(zero) = %q, want a dash", got)
	}
}

// TestErrorsUnwrapToTheTransport keeps errors.Is usable through the typed contract.
func TestErrorsUnwrapToTheTransport(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close()
	c, _ := NewClient(url, "", "", "1.0", time.Second)
	_, err := c.Clusters(context.Background(), ListOpts{})

	var target *errs.Error
	if !errors.As(err, &target) {
		t.Fatal("the transport error is not a typed one")
	}
	if target.Unwrap() == nil {
		t.Error("the original transport error is unreachable")
	}
}
