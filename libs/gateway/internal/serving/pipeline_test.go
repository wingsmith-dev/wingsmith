package serving

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func recordingStage(order *[]string, name string) stage {
	return func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		*order = append(*order, name)
		return r, true
	}
}

func TestThePipelineRunsEveryStageInOrderWhenNoneRefuses(t *testing.T) {
	var order []string
	stages := []stage{
		recordingStage(&order, "a"),
		recordingStage(&order, "b"),
		recordingStage(&order, "c"),
	}
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	runPipeline(stages, w, r)

	got := ""
	for _, name := range order {
		got += name
	}
	if got != "abc" {
		t.Fatalf("stages ran as %q, want \"abc\"", got)
	}
}

func TestThePipelineStopsAtTheFirstStageThatWritesAResponse(t *testing.T) {
	var order []string
	refusing := func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		order = append(order, "refuse")
		writeError(w, http.StatusBadRequest, "no", "refused")
		return r, false
	}
	stages := []stage{
		recordingStage(&order, "before"),
		refusing,
		recordingStage(&order, "after"),
	}
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	runPipeline(stages, w, r)

	if got := w.Code; got != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", got, http.StatusBadRequest)
	}
	for _, name := range order {
		if name == "after" {
			t.Fatalf("a stage ran after one that had already written the response: %v", order)
		}
	}
}

func TestAStageThatWrapsTheRequestCarriesItToTheNextOne(t *testing.T) {
	type ctxKey struct{}
	wrapping := func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		return r.WithContext(context.WithValue(r.Context(), ctxKey{}, "carried")), true
	}
	var seen any
	reading := func(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
		seen = r.Context().Value(ctxKey{})
		return r, true
	}
	stages := []stage{wrapping, reading}
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	runPipeline(stages, w, r)

	if seen != "carried" {
		t.Fatalf("later stage saw %v, want the value the earlier stage wrapped the request with", seen)
	}
}

func TestRoutingStagesOpenWithTheCatalog(t *testing.T) {
	stages := routingStages(stubResolver{})
	if len(stages) < 2 {
		t.Fatalf("expected more than one stage, got %d", len(stages))
	}
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	// The catalog stage is first: a GET /v1/models must be answered without
	// any of the body-parsing stages behind it ever running, since there is
	// no body to parse.
	next, ok := stages[0](w, r, &requestState{})
	if ok {
		t.Fatalf("stageCatalog should have stopped the pipeline for GET /v1/models")
	}
	_ = next
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
}
