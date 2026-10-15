package render

import (
	"strings"
	"testing"
)

func TestImageAllowedMatchesAtABoundary(t *testing.T) {
	prefixes := []string{"nvcr.io/nvidia/ai-dynamo", "ghcr.io/acme/"}
	cases := map[string]bool{
		"nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1":    true,
		"nvcr.io/nvidia/ai-dynamo:1.0":                   true,
		"nvcr.io/nvidia/ai-dynamo@sha256:abc":            true,
		"nvcr.io/nvidia/ai-dynamo-fork/vllm-runtime:1.0": false,
		"ghcr.io/acme/runtime:2":                         true,
		"docker.io/library/busybox:1":                    false,
	}
	for image, want := range cases {
		if got := imageAllowed(image, prefixes); got != want {
			t.Errorf("imageAllowed(%q) = %v, want %v", image, got, want)
		}
	}
	if !imageAllowed("anything:1", nil) {
		t.Error("an empty list must allow every image")
	}
}

func TestARenderOutsideTheAllowedImagesIsRefused(t *testing.T) {
	opts := testOptions()
	opts.ImagePrefixes = []string{"nvcr.io/nvidia/ai-dynamo"}
	if _, err := DynamoGraphDeployment(testInputs(), opts); err != nil {
		t.Fatalf("an allowed engine image was refused: %v", err)
	}

	opts.ImagePrefixes = []string{"ghcr.io/acme"}
	_, err := DynamoGraphDeployment(testInputs(), opts)
	if err == nil || !strings.Contains(err.Error(), prefetchImage) {
		t.Fatalf("error = %v, want a refusal naming %s", err, prefetchImage)
	}
	if _, err := PrefetchJob(hfArtifact(), prefetchImage, opts); err == nil {
		t.Error("the prefetch job ran an image outside the allowed prefixes")
	}
}

// An overlay is merged after the render, and an image it names is held to the same list.
func TestAnOverlayCannotBringItsOwnImage(t *testing.T) {
	opts := testOptions()
	opts.ImagePrefixes = []string{"nvcr.io/nvidia/ai-dynamo"}
	in := testInputs()
	in.Deployment.Serving.Overlay.PodSpec = map[string]any{
		"containers": []any{map[string]any{"name": "sidecar", "image": "docker.io/evil/miner:1"}},
	}
	_, err := DynamoGraphDeployment(in, opts)
	if err == nil || !strings.Contains(err.Error(), "docker.io/evil/miner:1") {
		t.Fatalf("error = %v, want the overlay's image refused", err)
	}
}
