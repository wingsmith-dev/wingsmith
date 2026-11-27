package cli

import "testing"

func TestContainerRuntimeFlags(t *testing.T) {
	if r, err := containerRuntime("dynamo", "", 8000, nil); r != nil || err != nil {
		t.Fatalf("dynamo = %+v, %v; want nil, nil", r, err)
	}
	if _, err := containerRuntime("container", "", 8000, nil); err == nil {
		t.Fatal("container without --image was accepted")
	}
	if _, err := containerRuntime("dynamo", "img", 8000, nil); err == nil {
		t.Fatal("--image without --runtime container was accepted")
	}
	if _, err := containerRuntime("wasm", "", 0, nil); err == nil {
		t.Fatal("an unknown runtime was accepted")
	}
	r, err := containerRuntime("container", "ghcr.io/a/w@sha256:x", 9000, []string{"--model", "$(WS_MODEL_PATH)"})
	if err != nil {
		t.Fatal(err)
	}
	c := r.Container
	if r.Kind != "container" || c.Image != "ghcr.io/a/w@sha256:x" || c.Ports[0].Name != "http" ||
		c.Ports[0].ContainerPort != 9000 || c.Args[1] != "$(WS_MODEL_PATH)" || c.Protocol != "openai-chat" {
		t.Fatalf("runtime = %+v / %+v", r, c)
	}
}
