package serving

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/metrics"
	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/plane"
	"github.com/wingsmith-dev/wingsmith/libs/gateway/internal/routing"
)

// audioServer answers with a fixed body and headers.
func audioServer(t *testing.T, headers map[string]string, body string) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		for k, v := range headers {
			w.Header().Set(k, v)
		}
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	return server
}

func generated(t *testing.T, recorder *metrics.Recorder) map[string]float64 {
	t.Helper()
	units := map[string]float64{}
	for _, m := range gathered(t, recorder, "wingsmith_gateway_generated_total") {
		units[seriesLabels(m)["unit"]] = m.Counter.GetValue()
	}
	return units
}

func TestSpeechMetersCharactersAndTheWorkersSeconds(t *testing.T) {
	server := audioServer(t, map[string]string{"Content-Type": "audio/mpeg", headerAudioSeconds: "3.5"}, "ID3\x00binary")
	recorder := metrics.New()
	handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: map[string]routing.Route{
		"tts": modalityRoute(t, "tts", server.URL, modalitySpeech),
	}}})
	rec := httptest.NewRecorder()
	lines := accessLines(t, func() {
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/audio/speech",
			strings.NewReader(`{"model":"tts","input":"héllo wörld","voice":"alloy"}`)))
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	if lines[0]["media"] != "speech" || lines[0]["speech_characters"] != 11.0 || lines[0]["audio_seconds"] != 3.5 {
		t.Fatalf("line = %v", lines[0])
	}
	if units := generated(t, recorder); units["speech_characters"] != 11 || units["speech_seconds"] != 3.5 {
		t.Fatalf("generated = %v", units)
	}
}

func TestTranscriptionMetersInputSecondsFromTheResponse(t *testing.T) {
	for name, body := range map[string]string{
		"verbose_json": `{"text":"hi","language":"en","duration":12.25,"segments":[]}`,
		"usage":        `{"text":"hi","usage":{"type":"duration","seconds":12.25}}`,
	} {
		t.Run(name, func(t *testing.T) {
			server := audioServer(t, map[string]string{"Content-Type": "application/json"}, body)
			recorder := metrics.New()
			handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: map[string]routing.Route{
				"whisper": modalityRoute(t, "whisper", server.URL, modalityTranscription),
			}}})
			form, contentType := transcriptionBody(t, "whisper")
			req := httptest.NewRequest(http.MethodPost, "/v1/audio/transcriptions", bytes.NewReader(form))
			req.Header.Set("Content-Type", contentType)
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			if rec.Code != http.StatusOK {
				t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
			}
			if units := generated(t, recorder); units["transcription_seconds"] != 12.25 {
				t.Fatalf("generated = %v", units)
			}
		})
	}
}

func TestAnImageIsMeteredBySizeTier(t *testing.T) {
	line, units := mediaCall(t, "image", "/v1/images/generations",
		`{"model":"gen","prompt":"a fox","size":"2048x2048","n":2}`, http.StatusOK,
		`{"created":1,"data":[{"url":"https://cdn.example/a.png"},{"url":"https://cdn.example/b.png"}]}`)
	if line["image_units"] != 8.0 || units["image_units"] != 8 || units["images"] != 2 {
		t.Fatalf("line = %v, generated = %v, want 2 images of tier 4", line, units)
	}
}

func TestAMediaPathHasItsOwnBodyCap(t *testing.T) {
	server, _ := upstreamServer(t)
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"tts": modalityRoute(t, "tts", server.URL, modalitySpeech),
	}}})
	big := `{"model":"tts","input":"` + strings.Repeat("a", 2<<20) + `"}`
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/audio/speech", strings.NewReader(big)))
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413", rec.Code)
	}
	if got := bodyCap("/v1/chat/completions", 0); got != DefaultMaxBodyBytes {
		t.Fatalf("chat cap = %d", got)
	}
	if got := bodyCap("/v1/audio/transcriptions", 1<<20); got != 1<<20 {
		t.Fatalf("a tighter listener cap wins, got %d", got)
	}
}

func TestAMediaRequestHasAnUpstreamDeadline(t *testing.T) {
	r, cancel := withUpstreamDeadline(httptest.NewRequest(http.MethodPost, "/v1/images/generations", nil))
	defer cancel()
	deadline, ok := r.Context().Deadline()
	if !ok || time.Until(deadline) > 121*time.Second || time.Until(deadline) < 100*time.Second {
		t.Fatalf("image deadline = %v, %v", deadline, ok)
	}
	r, cancel = withUpstreamDeadline(httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil))
	defer cancel()
	if _, ok := r.Context().Deadline(); ok {
		t.Fatal("a text request has no deadline")
	}
}

func quotaRoute(t *testing.T, address, modality string, limits *plane.Limits) routing.Route {
	route := modalityRoute(t, "tts", address, modality)
	route.ID = "end_tts"
	route.Limits = limits
	return route
}

func TestASpeechQuotaRefusesWhatTheBudgetCannotCover(t *testing.T) {
	server := audioServer(t, map[string]string{"Content-Type": "audio/mpeg"}, "audio")
	quotas := NewUnitQuotas(1)
	clock := time.Unix(0, 0)
	quotas.now = func() time.Time { return clock }
	route := quotaRoute(t, server.URL, modalitySpeech, &plane.Limits{
		UnitLimits: map[string]int64{"speech_characters": 10}, LimitScope: "per_replica", Enforced: true,
	})
	handler := New(Config{Zone: ZoneInternal, UnitQuotas: quotas, Router: stubResolver{routes: map[string]routing.Route{"tts": route}}})
	send := func(input string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/audio/speech",
			strings.NewReader(`{"model":"tts","input":"`+input+`"}`)))
		return rec
	}
	if rec := send("123456"); rec.Code != http.StatusOK {
		t.Fatalf("first = %d", rec.Code)
	}
	rec := send("12345")
	if rec.Code != http.StatusTooManyRequests || rec.Header().Get("Retry-After") == "" {
		t.Fatalf("over budget = %d %v", rec.Code, rec.Header())
	}
	clock = clock.Add(6 * time.Second) // one character back
	if rec := send("12345"); rec.Code != http.StatusOK {
		t.Fatalf("after refill = %d", rec.Code)
	}
}

func TestAnImageQuotaIsSpentAfterTheResponseAndRefusesTheNext(t *testing.T) {
	server := audioServer(t, map[string]string{"Content-Type": "application/json"},
		`{"data":[{"url":"https://cdn.example/a.png"},{"url":"https://cdn.example/b.png"}]}`)
	quotas := NewUnitQuotas(1)
	route := quotaRoute(t, server.URL, modalityImage, &plane.Limits{UnitLimits: map[string]int64{"image_units": 3}, Enforced: true})
	route.Name = "tts"
	handler := New(Config{Zone: ZoneInternal, Metrics: metrics.New(), UnitQuotas: quotas,
		Router: stubResolver{routes: map[string]routing.Route{"tts": route}}})
	codes := []int{}
	for range 3 {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/images/generations",
			strings.NewReader(`{"model":"tts","prompt":"x","size":"1024x1024"}`)))
		codes = append(codes, rec.Code)
	}
	if codes[0] != 200 || codes[1] != 200 || codes[2] != 429 {
		t.Fatalf("codes = %v, want two admitted (3 -> 1 -> -1) and the third refused", codes)
	}
}

func TestAShadowQuotaCountsButNeverRefuses(t *testing.T) {
	quotas := NewUnitQuotas(1)
	route := routing.Route{ID: "e", Limits: &plane.Limits{UnitLimits: map[string]int64{"speech_characters": 1}}}
	for range 3 {
		if _, ok := quotas.admit(route, mediaSpeech, map[string]float64{"speech_characters": 5}); !ok {
			t.Fatal("shadow mode refused")
		}
	}
}

func TestADividedQuotaIsThisReplicasShare(t *testing.T) {
	quotas := NewUnitQuotas(4)
	route := routing.Route{ID: "e", Limits: &plane.Limits{UnitLimits: map[string]int64{"video_seconds": 100}, LimitScope: "divided"}}
	if limit, _ := quotas.limitFor(route, "video_seconds"); limit != 25 {
		t.Fatalf("share = %v", limit)
	}
}
