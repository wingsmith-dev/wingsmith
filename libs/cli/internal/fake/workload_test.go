package fake

import (
	"context"
	"errors"
	"testing"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
)

// The plane refuses task and gang workloads at creation, since nothing renders
// them, so the fake does too.
func TestTaskAndGangWorkloadsAreRefusedAtCreation(t *testing.T) {
	b, err := New("")
	if err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"task", "gang"} {
		_, err := b.CreateWorkload(context.Background(), api.WorkloadCreateRequest{
			Name: "batch", Project: "core", Kind: kind, GangSize: 2, DesiredScale: 2,
			Serving: api.ServingRequest{Artifact: "llama-3.1-70b-instruct", Engine: "vllm", ContextLength: 8192},
		})
		var e *errs.Error
		if !errors.As(err, &e) || e.Code != errs.CodeInvalidState {
			t.Errorf("%s: err = %v, want INVALID_STATE", kind, err)
		}
	}
}
