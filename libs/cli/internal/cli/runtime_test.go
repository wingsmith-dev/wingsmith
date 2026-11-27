package cli

import "testing"

func TestContainerRuntimeFlags(t *testing.T) {
	if r, err := containerRuntime("dynamo", "", 8000, nil, "", "", ""); r != nil || err != nil {
		t.Fatalf("dynamo = %+v, %v; want nil, nil", r, err)
	}
	if _, err := containerRuntime("container", "", 8000, nil, "", "", ""); err == nil {
		t.Fatal("container without --image was accepted")
	}
	if _, err := containerRuntime("dynamo", "img", 8000, nil, "", "", ""); err == nil {
		t.Fatal("--image without --runtime container was accepted")
	}
	if _, err := containerRuntime("wasm", "", 0, nil, "", "", ""); err == nil {
		t.Fatal("an unknown runtime was accepted")
	}
	r, err := containerRuntime("container", "ghcr.io/a/w@sha256:x", 9000, []string{"--model", "$(WS_MODEL_PATH)"}, "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	c := r.Container
	if r.Kind != "container" || c.Image != "ghcr.io/a/w@sha256:x" || c.Ports[0].Name != "http" ||
		c.Ports[0].ContainerPort != 9000 || c.Args[1] != "$(WS_MODEL_PATH)" || c.Protocol != "openai-chat" {
		t.Fatalf("runtime = %+v / %+v", r, c)
	}
}

func TestContainerRuntimeProtocols(t *testing.T) {
	for _, protocol := range containerProtocols {
		r, err := containerRuntime("container", "img@sha256:x", 8000, nil, protocol, "", "")
		if err != nil || r.Container.Protocol != protocol {
			t.Fatalf("%s = %+v, %v", protocol, r, err)
		}
	}
	if _, err := containerRuntime("container", "img@sha256:x", 8000, nil, "grpc", "", ""); err == nil {
		t.Fatal("an unknown protocol was accepted")
	}
	if _, err := containerRuntime("dynamo", "", 8000, nil, "openai-audio-speech", "", ""); err == nil {
		t.Fatal("--protocol without --runtime container was accepted")
	}
	if _, err := omniBehaviour("speech", "", nil); err == nil {
		t.Fatal("omni audio was accepted")
	}
}

func TestPresetFlags(t *testing.T) {
	r, err := containerRuntime("dynamo", "", 8000, []string{"--max-model-len", "8192"}, "", "vllm", "0.34.0")
	if err != nil {
		t.Fatal(err)
	}
	c := r.Container
	if r.Kind != "container" || c.Preset != "vllm" || c.Version != "0.34.0" || c.Image != "" || c.Args[0] != "--max-model-len" {
		t.Fatalf("preset runtime = %+v / %+v", r, c)
	}
	for name, call := range map[string][]string{
		"unknown preset":    {"dynamo", "", "tgi", ""},
		"preset with image": {"container", "img@sha256:x", "vllm", ""},
		"version alone":     {"dynamo", "", "", "0.34.0"},
		"preset on wasm":    {"wasm", "", "vllm", ""},
	} {
		if _, err := containerRuntime(call[0], call[1], 8000, nil, "", call[2], call[3]); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}
