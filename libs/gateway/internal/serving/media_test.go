package serving

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

// mediaCall sends one request at an upstream that answers with status and body, and returns the
// access line and the generated_total series by unit.
func mediaCall(t *testing.T, modality, path, request string, status int, response string) (map[string]any, map[string]float64) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		for chunk := []byte(response); len(chunk) > 0; {
			n := min(len(chunk), 4093)
			_, _ = w.Write(chunk[:n])
			w.(http.Flusher).Flush()
			chunk = chunk[n:]
		}
	}))
	t.Cleanup(server.Close)
	recorder := metrics.New()
	handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: map[string]routing.Route{
		"gen": modalityRoute(t, "gen", server.URL, modality),
	}}})

	rec := httptest.NewRecorder()
	lines := accessLines(t, func() {
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, path, strings.NewReader(request)))
	})
	if rec.Body.Len() != len(response) && status < 300 {
		t.Fatalf("forwarded %d bytes, want %d", rec.Body.Len(), len(response))
	}
	if len(lines) != 1 {
		t.Fatalf("access lines = %v", lines)
	}
	units := map[string]float64{}
	for _, m := range gathered(t, recorder, "hangar_gateway_generated_total") {
		units[seriesLabels(m)["unit"]] = m.Counter.GetValue()
	}
	return lines[0], units
}

func b64(n int) string { return strings.Repeat("QUJD", n/4) }

// TestAnImageGenerationRecordsItsOutputsWithoutHoldingThem: two images several times the size of
// the usage window are counted as they pass, and the size the response states is recorded as the
// response's, because the request did not state one.
func TestAnImageGenerationRecordsItsOutputsWithoutHoldingThem(t *testing.T) {
	response := `{"created":1,"data":[{"b64_json":"` + b64(4*usageTailBytes) + `"},{"b64_json":"` +
		b64(4*usageTailBytes) + `"}],"size":"1024x1024"}`
	line, units := mediaCall(t, "image", "/v1/images/generations",
		`{"model":"gen","prompt":"a fox","n":2}`, http.StatusOK, response)

	if line["media"] != "image" || line["images"] != 2.0 || line["requested_n"] != 2.0 {
		t.Fatalf("line = %v, want media=image images=2 requested_n=2", line)
	}
	if line["size"] != "1024x1024" || line["size_source"] != "response" {
		t.Fatalf("line = %v, want the response's size, marked as the response's", line)
	}
	if _, ok := line["upstream_ms"]; !ok {
		t.Fatalf("line = %v, want upstream_ms", line)
	}
	if units["images"] != 2 || units["upstream_seconds"] <= 0 {
		t.Fatalf("generated = %v, want images=2 and upstream time", units)
	}
}

func TestARequestedImageSizeIsRecordedAsTheRequests(t *testing.T) {
	line, _ := mediaCall(t, "image", "/v1/images/generations",
		`{"model":"gen","prompt":"a fox","size":"1328x1328"}`, http.StatusOK,
		`{"created":1,"data":[{"url":"https://cdn.example/x.png"}]}`)
	if line["images"] != 1.0 || line["size"] != "1328x1328" || line["size_source"] != "request" {
		t.Fatalf("line = %v", line)
	}
}

// TestAnImageAnsweredAsAChatCompletionCountsItsImageParts is the chat surface of an image model.
func TestAnImageAnsweredAsAChatCompletionCountsItsImageParts(t *testing.T) {
	response := `{"choices":[{"message":{"role":"assistant","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,` +
		b64(1<<20) + `"}}]}}]}`
	line, units := mediaCall(t, "image", "/v1/chat/completions",
		`{"model":"gen","messages":[{"role":"user","content":"a fox"}]}`, http.StatusOK, response)
	if line["media"] != "image" || line["images"] != 1.0 || units["images"] != 1 {
		t.Fatalf("line = %v, generated = %v", line, units)
	}
}

func TestATextChatCompletionIsNotMedia(t *testing.T) {
	line, units := mediaCall(t, "text", "/v1/chat/completions", `{"model":"gen","messages":[]}`,
		http.StatusOK, `{"choices":[{"message":{"content":"see \"url\":\"x\""}}],"usage":{"prompt_tokens":3,"completion_tokens":2}}`)
	if _, ok := line["media"]; ok || len(units) != 0 {
		t.Fatalf("line = %v, generated = %v, want no media facts", line, units)
	}
}

// TestAVideoRecordsItsLengthAsTheRequestStatedIt, in each of the spellings a request can use.
func TestAVideoRecordsItsLengthAsTheRequestStatedIt(t *testing.T) {
	response := `{"id":"v","object":"video","model":"gen","status":"completed","progress":100,"created":1,` +
		`"data":[{"output_format":"mp4","b64_json":"` + b64(2*usageTailBytes) + `"}],"inference_time_s":42.5}`
	for _, test := range []struct {
		name    string
		request string
		seconds any
		frames  any
	}{
		{"seconds as OpenAI spells it", `{"model":"gen","prompt":"x","seconds":"4"}`, 4.0, nil},
		{"seconds and fps", `{"model":"gen","prompt":"x","seconds":5,"nvext":{"fps":16}}`, 5.0, 80.0},
		{"frames and fps", `{"model":"gen","prompt":"x","nvext":{"num_frames":81,"fps":16}}`, 81.0 / 16.0, 81.0},
		{"nothing stated", `{"model":"gen","prompt":"x"}`, nil, nil},
	} {
		t.Run(test.name, func(t *testing.T) {
			line, units := mediaCall(t, "video", "/v1/videos", test.request, http.StatusOK, response)
			if line["media"] != "video" || line["videos"] != 1.0 || line["inference_time_s"] != 42.5 {
				t.Fatalf("line = %v", line)
			}
			if line["video_seconds"] != test.seconds || line["video_frames"] != test.frames {
				t.Fatalf("seconds = %v frames = %v, want %v and %v", line["video_seconds"], line["video_frames"], test.seconds, test.frames)
			}
			if units["videos"] != 1 {
				t.Fatalf("generated = %v", units)
			}
		})
	}
}

// TestAFailedGenerationRecordsNothingGenerated because a refusal produced nothing.
func TestAFailedGenerationRecordsNothingGenerated(t *testing.T) {
	line, units := mediaCall(t, "image", "/v1/images/generations", `{"model":"gen","prompt":"x","size":"1368x768"}`,
		http.StatusBadRequest, `{"message":"Width must be divisible by 16 (got 1368)","type":"Bad Request","code":400}`)
	if _, ok := line["media"]; ok || len(units) != 0 {
		t.Fatalf("line = %v, generated = %v, want nothing recorded", line, units)
	}
}

// TestTheOutputCounterFindsANeedleSplitAcrossReads at every split point, which is what a TCP read
// boundary does to it.
func TestTheOutputCounterFindsANeedleSplitAcrossReads(t *testing.T) {
	body := []byte(`{"data":[{"b64_json":"AAAA"},{"url":"u"},{"b64_json":"BB"}],"x":"\"url\":\"no"}`)
	for step := 1; step <= len(body); step++ {
		counter := newMediaCounter(false)
		for i := 0; i < len(body); i += step {
			counter.write(body[i:min(len(body), i+step)])
		}
		if counter.count != 3 {
			t.Fatalf("reads of %d bytes counted %d, want 3", step, counter.count)
		}
	}
}
