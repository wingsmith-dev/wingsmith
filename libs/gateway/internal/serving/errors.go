package serving

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"sort"
	"strconv"
)

const (
	maxStopSequences        = 4
	maxUpstreamErrorBodyLen = 64 << 10
)

// errorEnvelope is the OpenAI error shape. Clients of an OpenAI-compatible API
// parse it, so an error from this hop has to look like an error from the thing
// it stands in front of.
type errorEnvelope struct {
	Error errorBody `json:"error"`
}

type errorBody struct {
	Message string `json:"message"`
	Type    string `json:"type"`
	Code    string `json:"code"`
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(errorEnvelope{
		Error: errorBody{Message: message, Type: "invalid_request_error", Code: code},
	})
}

// tooManyStopSequences enforces the OpenAI completion limit before a request
// reaches an engine. Dynamo currently turns this client mistake into a generic
// 500, which makes a retryable-looking server failure out of a stable request.
func tooManyStopSequences(path string, raw json.RawMessage) bool {
	if (path != "/v1/chat/completions" && path != "/v1/completions") || len(raw) == 0 {
		return false
	}
	var values []json.RawMessage
	if err := json.Unmarshal(raw, &values); err != nil {
		return false
	}
	return len(values) > maxStopSequences
}

// modifyResponse keeps successful responses on the existing streaming usage path and
// normalises JSON errors before they leave Hangar's public hop.
func modifyResponse(resp *http.Response) error {
	if err := normalizeErrorResponse(resp); err != nil {
		return err
	}
	return observeUsage(resp)
}

// normalizeErrorResponse wraps Dynamo's top-level error object in the OpenAI
// `error` envelope. Errors that already use that envelope, non-JSON errors, and
// oversized bodies pass through unchanged.
func normalizeErrorResponse(resp *http.Response) error {
	if resp.StatusCode < http.StatusBadRequest || resp.Body == nil {
		return nil
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxUpstreamErrorBodyLen+1))
	if err != nil {
		return err
	}
	if len(body) > maxUpstreamErrorBodyLen {
		resp.Body = &joinedReadCloser{
			Reader: io.MultiReader(bytes.NewReader(body), resp.Body),
			Closer: resp.Body,
		}
		return nil
	}
	if err := resp.Body.Close(); err != nil {
		return err
	}

	setBody := func(value []byte) {
		resp.Body = io.NopCloser(bytes.NewReader(value))
		resp.ContentLength = int64(len(value))
		resp.Header.Set("Content-Length", strconv.Itoa(len(value)))
	}

	var object map[string]json.RawMessage
	if err := json.Unmarshal(body, &object); err != nil {
		setBody(body)
		return nil
	}
	if _, nested := object["error"]; nested {
		setBody(body)
		return nil
	}
	if _, hasMessage := object["message"]; !hasMessage {
		setBody(body)
		return nil
	}

	wrapped, err := json.Marshal(struct {
		Error json.RawMessage `json:"error"`
	}{Error: body})
	if err != nil {
		setBody(body)
		return nil
	}
	setBody(wrapped)
	return nil
}

type joinedReadCloser struct {
	io.Reader
	io.Closer
}

type modelObject struct {
	ID      string `json:"id"`
	Object  string `json:"object"`
	OwnedBy string `json:"owned_by"`
}

type modelList struct {
	Object string        `json:"object"`
	Data   []modelObject `json:"data"`
}

// writeModelList answers GET /v1/models from the routing table.
//
// Deliberately bare: context length, quantization and display names are catalog
// facts, and hangar serves models rather than describing them. Whatever sits in
// front of this hop owns its own catalog.
func writeModelList(w http.ResponseWriter, names []string) {
	sort.Strings(names)
	data := make([]modelObject, 0, len(names))
	for _, name := range names {
		data = append(data, modelObject{ID: name, Object: "model", OwnedBy: "hangar"})
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(modelList{Object: "list", Data: data})
}
