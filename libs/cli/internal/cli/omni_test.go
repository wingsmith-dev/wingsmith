package cli

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
)

// A request with no serving flags must leave the plane's defaults alone. Sent as zero values,
// the behaviour and capability blocks decoded as an empty transport and null modality lists,
// which the plane refuses.
func TestAPlainServingRequestSendsNoBehaviourOrCapabilities(t *testing.T) {
	body, err := json.Marshal(api.ServingRequest{Artifact: "a", Engine: "vllm", ContextLength: 8})
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{`"serving"`, `"capabilities"`} {
		if strings.Contains(string(body), key) {
			t.Fatalf("request carries %s with nothing set: %s", key, body)
		}
	}
}

func TestOmniBehaviour(t *testing.T) {
	b, err := omniBehaviour("image", "", []string{"num_inference_steps=8", "guidance_scale=4.0", "scheduler=euler"})
	if err != nil {
		t.Fatal(err)
	}
	if b.OutputModality == nil || *b.OutputModality != "image" || b.Reasoning.Transport != "reasoning_content" {
		t.Fatalf("behaviour = %+v", b)
	}
	got := b.Diffusion.DefaultSamplingParams
	if got["num_inference_steps"] != float64(8) || got["guidance_scale"] != 4.0 || got["scheduler"] != "euler" {
		t.Fatalf("sampling defaults = %#v", got)
	}
	if b.Diffusion.ModelClassName != nil {
		t.Fatalf("a model class nobody named was sent: %v", *b.Diffusion.ModelClassName)
	}
	if _, err := omniBehaviour("audio", "", nil); err == nil {
		t.Fatal("audio accepted as an output modality")
	}
	if b, _ := omniBehaviour("", "", nil); b != nil {
		t.Fatal("a chat workload sent a behaviour block")
	}
}

func TestAnOmniWorkloadIsCreatedWithoutAContextLength(t *testing.T) {
	t.Setenv("HANGAR_FAKE_STATE", filepath.Join(t.TempDir(), "state.json"))
	out, errOut, code := run(t, "workload", "create", "z-image-turbo",
		"--artifact", "llama-3.1-70b", "--project", "core", "--class", "h100-80gb-sxm",
		"--engine", "omni", "--engine-runtime-version", "1.4.1",
		"--output-modality", "image", "--sampling-default", "num_inference_steps=8", "-o", "json")
	if code != 0 {
		t.Fatalf("create exit %d: %s %s", code, out, errOut)
	}
	var wl api.Workload
	if err := json.Unmarshal([]byte(out), &wl); err != nil {
		t.Fatalf("create is not a workload: %v", err)
	}
	b := wl.Serving.Behaviour
	if b.OutputModality == nil || *b.OutputModality != "image" || b.Diffusion == nil {
		t.Fatalf("serving block = %+v", b)
	}
}

func TestOmniFlagsAreRefusedOnAChatEngine(t *testing.T) {
	_, errOut, code := run(t, "workload", "create", "chat2",
		"--artifact", "llama-3.1-70b", "--project", "core", "--class", "h100-80gb-sxm",
		"--context-length", "8192", "--output-modality", "image")
	if code == 0 || !strings.Contains(errOut, "--engine omni") {
		t.Fatalf("exit %d: %s", code, errOut)
	}
	_, errOut, code = run(t, "workload", "create", "img",
		"--artifact", "llama-3.1-70b", "--project", "core", "--class", "h100-80gb-sxm", "--engine", "omni")
	if code == 0 || !strings.Contains(errOut, "--output-modality") {
		t.Fatalf("omni without a modality: exit %d: %s", code, errOut)
	}
}
