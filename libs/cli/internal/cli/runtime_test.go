package cli

import "testing"

func TestContainerRuntimeFlags(t *testing.T) {
	if r, err := containerRuntime("dynamo", "", 8000, nil, ""); r != nil || err != nil {
		t.Fatalf("dynamo = %+v, %v; want nil, nil", r, err)
	}
	if _, err := containerRuntime("container", "", 8000, nil, ""); err == nil {
		t.Fatal("container without --image was accepted")
	}
	if _, err := containerRuntime("dynamo", "img", 8000, nil, ""); err == nil {
		t.Fatal("--image without --runtime container was accepted")
	}
	if _, err := containerRuntime("wasm", "", 0, nil, ""); err == nil {
		t.Fatal("an unknown runtime was accepted")
	}
	r, err := containerRuntime("container", "ghcr.io/a/w@sha256:x", 9000, []string{"--model", "$(WS_MODEL_PATH)"}, "")
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
		r, err := containerRuntime("container", "img@sha256:x", 8000, nil, protocol)
		if err != nil || r.Container.Protocol != protocol {
			t.Fatalf("%s = %+v, %v", protocol, r, err)
		}
	}
	if _, err := containerRuntime("container", "img@sha256:x", 8000, nil, "grpc"); err == nil {
		t.Fatal("an unknown protocol was accepted")
	}
	if _, err := containerRuntime("dynamo", "", 8000, nil, "openai-audio-speech"); err == nil {
		t.Fatal("--protocol without --runtime container was accepted")
	}
	if _, err := omniBehaviour("speech", "", nil); err == nil {
		t.Fatal("omni audio was accepted")
	}
}
