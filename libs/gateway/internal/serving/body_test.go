package serving

import (
	"bufio"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

func chatHandler(t *testing.T, cfg Config) http.Handler {
	t.Helper()
	server, _ := upstreamServer(t)
	cfg.Zone = ZoneInternal
	cfg.Router = stubResolver{routes: map[string]routing.Route{"chat": routeTo(t, server.URL, nil)}}
	return New(cfg)
}

func TestTheBodyCapIsConfigurable(t *testing.T) {
	handler := chatHandler(t, Config{MaxBodyBytes: 32})
	for _, tc := range []struct {
		body   string
		status int
	}{
		{`{"model":"chat"}`, http.StatusOK},
		{`{"model":"chat","messages":[{"role":"user","content":"too long"}]}`, http.StatusRequestEntityTooLarge},
	} {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(tc.body)))
		if rec.Code != tc.status {
			t.Errorf("%d-byte body: status = %d, want %d", len(tc.body), rec.Code, tc.status)
		}
	}
}

// TestTheBufferBudgetRefusesAndReturnsItsBytes pins both halves: a body that
// would take the total past the budget is a 503, and every byte charged comes
// back, whether the request was refused or served.
func TestTheBufferBudgetRefusesAndReturnsItsBytes(t *testing.T) {
	budget := NewBodyBudget(24)
	handler := chatHandler(t, Config{BodyBudget: budget})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
		strings.NewReader(`{"model":"chat","messages":[]}`)))
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "no_capacity") {
		t.Fatalf("status = %d, body = %s, want 503 no_capacity", rec.Code, rec.Body)
	}
	if used := budget.Used(); used != 0 {
		t.Fatalf("a refused body left %d bytes charged", used)
	}

	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"chat"}`)))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 inside the budget", rec.Code)
	}
	if used := budget.Used(); used != 0 {
		t.Fatalf("a finished request left %d bytes charged", used)
	}
}

// TestABodyThatStallsIsAnsweredWithATimeout sends the headers and half a body,
// then stops. The read deadline covers the body only.
func TestABodyThatStallsIsAnsweredWithATimeout(t *testing.T) {
	server := httptest.NewServer(chatHandler(t, Config{BodyReadTimeout: 100 * time.Millisecond}))
	defer server.Close()

	conn, err := net.Dial("tcp", server.Listener.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close() }()
	_, _ = fmt.Fprintf(conn, "POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"model\":")
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatalf("read response: %v", err)
	}
	if resp.StatusCode != http.StatusRequestTimeout {
		t.Fatalf("status = %d, want 408", resp.StatusCode)
	}
}

// TestAKeptAliveConnectionOutlivesTheWriteDeadline is the regression the
// per-write deadline invites: armed during one response and left in place, it
// would fail the next response on the same connection.
func TestAKeptAliveConnectionOutlivesTheWriteDeadline(t *testing.T) {
	server := httptest.NewServer(chatHandler(t, Config{WriteIdleTimeout: 50 * time.Millisecond}))
	defer server.Close()

	client := server.Client()
	for i := 0; i < 2; i++ {
		resp, err := client.Post(server.URL+"/v1/chat/completions", "application/json", strings.NewReader(`{"model":"chat"}`))
		if err != nil {
			t.Fatalf("request %d: %v", i, err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("request %d: status = %d", i, resp.StatusCode)
		}
		time.Sleep(120 * time.Millisecond)
	}
}
