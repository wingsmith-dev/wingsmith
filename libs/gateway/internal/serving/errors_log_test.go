package serving

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/routing"
)

const callerSecret = "the quarterly numbers for project nightingale"

// upstreamErrorLog sends one completion carrying callerSecret at an upstream
// that answers status and body, and returns the single upstream-error log line
// the gateway wrote for it.
func upstreamErrorLog(t *testing.T, status int, body string, logBodies bool) map[string]any {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)

	var buf bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(previous) })

	handler := New(Config{
		Zone:              ZoneInternal,
		Router:            stubResolver{routes: map[string]routing.Route{"chat": routeTo(t, server.URL, nil)}},
		LogUpstreamBodies: logBodies,
	})
	caller := `{"model":"chat","messages":[{"role":"user","content":"` + callerSecret + `"}]}`
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(caller)))

	for _, line := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		var entry map[string]any
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatalf("decode log line %q: %v", line, err)
		}
		if strings.HasPrefix(entry["msg"].(string), "gateway: upstream error") {
			entry["_raw"] = buf.String()
			return entry
		}
	}
	t.Fatalf("no upstream error line in:\n%s", buf.String())
	return nil
}

func TestAnEngineRefusalThatQuotesThePromptNeverReachesTheLog(t *testing.T) {
	body := `{"object":"error","message":"could not parse '` + callerSecret + `'","type":"BadRequestError","param":null,"code":400}`
	entry := upstreamErrorLog(t, http.StatusBadRequest, body, false)

	if raw := entry["_raw"].(string); strings.Contains(raw, "nightingale") {
		t.Fatalf("the caller's content reached the log:\n%s", raw)
	}
	if _, present := entry["upstream_body"]; present {
		t.Fatalf("upstream_body logged without the debug switch: %v", entry)
	}
	if entry["status"] != float64(http.StatusBadRequest) {
		t.Errorf("status = %v, want 400", entry["status"])
	}
	if entry["engine_error_type"] != "BadRequestError" || entry["engine_error_code"] != "400" {
		t.Errorf("engine identity = %v / %v, want BadRequestError / 400", entry["engine_error_type"], entry["engine_error_code"])
	}
	if entry["body_bytes"] != float64(len(body)) || entry["body_truncated"] != false {
		t.Errorf("body summary = %v bytes truncated=%v, want %d false", entry["body_bytes"], entry["body_truncated"], len(body))
	}
}

func TestAServerTraceCarryingTheRequestNeverReachesTheLog(t *testing.T) {
	trace := "Traceback (most recent call last):\n  File \"/opt/dynamo/handler.py\"\nValueError: prompt=" + callerSecret
	entry := upstreamErrorLog(t, http.StatusInternalServerError, trace, false)

	if raw := entry["_raw"].(string); strings.Contains(raw, "nightingale") || strings.Contains(raw, "Traceback") {
		t.Fatalf("the upstream body reached the log:\n%s", raw)
	}
	if entry["level"] != "ERROR" {
		t.Errorf("level = %v, want ERROR for a 5xx", entry["level"])
	}
	if _, present := entry["engine_error_type"]; present {
		t.Errorf("a prose body produced an engine type: %v", entry["engine_error_type"])
	}
}

func TestAnEngineTypeThatIsProseIsDroppedRatherThanTruncated(t *testing.T) {
	body := `{"error":{"message":"bad","type":"` + callerSecret + `","code":"invalid_value"}}`
	entry := upstreamErrorLog(t, http.StatusBadRequest, body, false)

	if _, present := entry["engine_error_type"]; present {
		t.Errorf("prose in `type` reached the log: %v", entry["engine_error_type"])
	}
	if entry["engine_error_code"] != "invalid_value" {
		t.Errorf("engine_error_code = %v, want invalid_value", entry["engine_error_code"])
	}
}

func TestABodyAtTheReadCapIsReportedTruncated(t *testing.T) {
	body := strings.Repeat("x", maxLoggedRefusalLen*2)
	entry := upstreamErrorLog(t, http.StatusBadRequest, body, false)

	if entry["body_bytes"] != float64(maxLoggedRefusalLen) || entry["body_truncated"] != true {
		t.Errorf("body summary = %v truncated=%v, want %d true", entry["body_bytes"], entry["body_truncated"], maxLoggedRefusalLen)
	}
}

func TestTheDebugSwitchLogsTheWholeBody(t *testing.T) {
	body := `{"message":"could not parse '` + callerSecret + `'","code":400}`
	entry := upstreamErrorLog(t, http.StatusBadRequest, body, true)

	if entry["upstream_body"] != body {
		t.Errorf("upstream_body = %v, want the body as the engine sent it", entry["upstream_body"])
	}
}
