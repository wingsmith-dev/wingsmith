package gateway

import (
	"encoding/json"
	"net/http"
	"sort"
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
