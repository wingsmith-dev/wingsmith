package serving

import (
	"bytes"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

// transcriptionBody builds the shape OpenAI's transcription clients send: an
// audio file part and a `model` form field beside it.
func transcriptionBody(t *testing.T, model string) (body []byte, contentType string) {
	t.Helper()
	var buffer bytes.Buffer
	writer := multipart.NewWriter(&buffer)
	file, err := writer.CreateFormFile("file", "meeting.wav")
	if err != nil {
		t.Fatalf("create file part: %v", err)
	}
	if _, err := file.Write([]byte("RIFF....WAVEfmt ")); err != nil {
		t.Fatalf("write file part: %v", err)
	}
	if model != "" {
		if err := writer.WriteField("model", model); err != nil {
			t.Fatalf("write model field: %v", err)
		}
	}
	if err := writer.WriteField("response_format", "verbose_json"); err != nil {
		t.Fatalf("write response_format: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close writer: %v", err)
	}
	return buffer.Bytes(), writer.FormDataContentType()
}

// recordingUpstream keeps the request it was given, which is the only place the
// forwarded body and its content type can both be seen.
func recordingUpstream(t *testing.T) (*httptest.Server, *http.Request, *[]byte) {
	t.Helper()
	seen := &http.Request{}
	body := new([]byte)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		read, _ := io.ReadAll(r.Body)
		*body = read
		*seen = *r
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(server.Close)
	return server, seen, body
}

func multipartRequest(path string, body []byte, contentType string) *http.Request {
	request := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
	request.Header.Set("Content-Type", contentType)
	return request
}

func multipartRoute(t *testing.T, address string, servedModelName string) routing.Route {
	t.Helper()
	parsed, err := url.Parse(address)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	route := routing.NewRoute("whisper", "weighted",
		routing.NewUpstream("ap", 100, true, NewReverseProxy(parsed, nil, 0)))
	route.Upstreams[0].ServedModelName = servedModelName
	return route
}

func fieldsOf(t *testing.T, body []byte, contentType string) (map[string]string, map[string]string) {
	t.Helper()
	_, params, err := mime.ParseMediaType(contentType)
	if err != nil {
		t.Fatalf("content type %q: %v", contentType, err)
	}
	reader := multipart.NewReader(bytes.NewReader(body), params["boundary"])
	values := map[string]string{}
	files := map[string]string{}
	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("next part: %v", err)
		}
		content, _ := io.ReadAll(part)
		if part.FileName() != "" {
			files[part.FileName()] = string(content)
			continue
		}
		values[part.FormName()] = string(content)
	}
	return values, files
}

func TestAMultipartTranscriptionRoutesOnItsModelField(t *testing.T) {
	server, _, forwarded := recordingUpstream(t)
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"whisper": multipartRoute(t, server.URL, ""),
	}}})

	body, contentType := transcriptionBody(t, "whisper")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, multipartRequest("/v1/audio/transcriptions", body, contentType))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	if !bytes.Equal(*forwarded, body) {
		t.Fatalf("body was not forwarded byte-identically when there is nothing to rewrite")
	}
}

func TestAMultipartModelIsRewrittenToTheNameTheEngineAnswersTo(t *testing.T) {
	server, seen, forwarded := recordingUpstream(t)
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"whisper": multipartRoute(t, server.URL, "openai-whisper-large-v3"),
	}}})

	body, contentType := transcriptionBody(t, "whisper")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, multipartRequest("/v1/audio/transcriptions", body, contentType))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	values, files := fieldsOf(t, *forwarded, seen.Header.Get("Content-Type"))
	if values["model"] != "openai-whisper-large-v3" {
		t.Fatalf("model = %q, want the served name", values["model"])
	}
	if values["response_format"] != "verbose_json" {
		t.Fatalf("response_format = %q, want every other field carried through", values["response_format"])
	}
	if files["meeting.wav"] != "RIFF....WAVEfmt " {
		t.Fatalf("file part = %q, want the upload intact under its own filename", files["meeting.wav"])
	}
}

func TestAMultipartRequestWithNoModelFieldIsRejectedAsMissingModel(t *testing.T) {
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"whisper": routing.NewRoute("whisper", "weighted"),
	}}})

	body, contentType := transcriptionBody(t, "")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, multipartRequest("/v1/audio/transcriptions", body, contentType))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "missing_model") {
		t.Fatalf("body = %s, want missing_model", rec.Body.String())
	}
}

func TestAnUnreadableMultipartBodyIsNotReportedAsBadJSON(t *testing.T) {
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: map[string]routing.Route{
		"whisper": routing.NewRoute("whisper", "weighted"),
	}}})

	for name, contentType := range map[string]string{
		"no boundary":    "multipart/form-data",
		"wrong boundary": "multipart/form-data; boundary=nothing-matches-this",
	} {
		rec := httptest.NewRecorder()
		body, _ := transcriptionBody(t, "whisper")
		handler.ServeHTTP(rec, multipartRequest("/v1/audio/transcriptions", body, contentType))

		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s: status = %d, want 400", name, rec.Code)
		}
		if !strings.Contains(rec.Body.String(), "invalid_multipart") {
			t.Fatalf("%s: body = %s, want invalid_multipart", name, rec.Body.String())
		}
	}
}

func TestAJSONBodyIsUnaffectedByTheMultipartPath(t *testing.T) {
	if _, ok := multipartContentType("application/json"); ok {
		t.Fatalf("application/json was read as multipart")
	}
	if _, ok := multipartContentType(""); ok {
		t.Fatalf("an absent content type was read as multipart")
	}
}
