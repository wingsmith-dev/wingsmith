package render

import (
	"encoding/json"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

const prefetchImage = "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1"

func testOptions() Options {
	return Options{
		Namespace:      "dynamo",
		PoolLabelKey:   "hangar.io/pool",
		GPUPoolValue:   "gpu",
		CachePoolValue: "cache",
		CacheTaintKey:  "hangar.io/cache",
		ModelCachePVC:  "model-cache",
	}
}

func hfArtifact() wire.ArtifactDesired {
	return wire.ArtifactDesired{
		ArtifactID: "art_01J9X2AAAAAAAAAAAAAAAAAAAA",
		Digest:     "sha256:abc",
		Sources: []wire.ArtifactSource{
			{Kind: "s3", URI: "s3://weights/qwen", Priority: 1},
			{Kind: "hf", URI: "hf://Qwen/Qwen3-8B", Priority: 0},
		},
	}
}

func envValue(c corev1.Container, name string) (string, bool) {
	for _, e := range c.Env {
		if e.Name == name {
			return e.Value, true
		}
	}
	return "", false
}

func TestPrefetchJobHuggingFace(t *testing.T) {
	job, err := PrefetchJob(hfArtifact(), prefetchImage, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}

	if job.Namespace != "dynamo" {
		t.Fatalf("namespace = %q", job.Namespace)
	}
	if job.Name != "prefetch-art-01j9x2aaaaaaaaaaaaaaaaaaaa" {
		t.Fatalf("name = %q", job.Name)
	}
	if got := job.Labels["hangar.dev/artifact"]; got != hfArtifact().ArtifactID {
		t.Fatalf("artifact label = %q", got)
	}
	if got := job.Labels["hangar.dev/managed-by"]; got != "hangar-operator" {
		t.Fatalf("managed-by label = %q", got)
	}
	if job.Spec.BackoffLimit == nil || *job.Spec.BackoffLimit != 3 {
		t.Fatalf("backoff limit = %v", job.Spec.BackoffLimit)
	}

	pod := job.Spec.Template.Spec
	if pod.RestartPolicy != corev1.RestartPolicyOnFailure {
		t.Fatalf("restart policy = %q", pod.RestartPolicy)
	}
	if got := pod.NodeSelector["hangar.io/pool"]; got != "cache" {
		t.Fatalf("node selector pool = %q, want the cache pool", got)
	}
	if len(pod.Tolerations) != 1 || pod.Tolerations[0].Key != "hangar.io/cache" {
		t.Fatalf("tolerations = %+v", pod.Tolerations)
	}
	if len(pod.Volumes) != 1 || pod.Volumes[0].PersistentVolumeClaim == nil ||
		pod.Volumes[0].PersistentVolumeClaim.ClaimName != "model-cache" {
		t.Fatalf("volumes = %+v", pod.Volumes)
	}

	c := pod.Containers[0]
	if c.Image != prefetchImage {
		t.Fatalf("image = %q, want the worker runtime image", c.Image)
	}
	if strings.Join(c.Command, " ") != "python3 -c" {
		t.Fatalf("command = %v", c.Command)
	}
	if len(c.Args) != 1 || !strings.Contains(c.Args[0], "snapshot_download") {
		t.Fatalf("args do not call snapshot_download: %v", c.Args)
	}
	if !strings.Contains(c.Args[0], `"Qwen/Qwen3-8B"`) {
		t.Fatalf("args do not name the repo id: %v", c.Args)
	}
	if !strings.Contains(c.Args[0], "max_workers=8") {
		t.Fatalf("args do not bound parallelism: %v", c.Args)
	}
	if v, ok := envValue(c, "HF_HOME"); !ok || v != "/models/hf" {
		t.Fatalf("HF_HOME = %q, %v", v, ok)
	}
	if v, ok := envValue(c, "HF_HUB_DISABLE_PROGRESS_BARS"); !ok || v != "1" {
		t.Fatalf("HF_HUB_DISABLE_PROGRESS_BARS = %q, %v", v, ok)
	}
	if len(c.VolumeMounts) != 1 || c.VolumeMounts[0].MountPath != "/models" {
		t.Fatalf("volume mounts = %+v", c.VolumeMounts)
	}
}

// TestPrefetchJobCustomWeights covers the artifact that has no HuggingFace source. Custom weights
// are a first-class case, so it must render a job rather than refuse, and it must land them at the
// same path modelRef points --model at.
func TestPrefetchJobCustomWeights(t *testing.T) {
	art := wire.ArtifactDesired{
		ArtifactID: "art_custom",
		Sources: []wire.ArtifactSource{
			{Kind: "https", URI: "https://example.invalid/backup.safetensors", Priority: 5},
			{Kind: "s3", URI: "s3://weights/custom/model.safetensors", Priority: 0},
		},
	}
	job, err := PrefetchJob(art, prefetchImage, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	args := job.Spec.Template.Spec.Containers[0].Args[0]
	if strings.Contains(args, "snapshot_download") {
		t.Fatalf("non-HF artifact rendered the HuggingFace strategy: %s", args)
	}
	if !strings.Contains(args, `"s3://weights/custom/model.safetensors"`) {
		t.Fatalf("lowest-priority source did not win: %s", args)
	}
	if !strings.Contains(args, `"/models/art_custom"`) {
		t.Fatalf("weights do not land where modelRef looks: %s", args)
	}

	ref, err := modelRef(art)
	if err != nil {
		t.Fatalf("modelRef: %v", err)
	}
	if !strings.Contains(args, `"`+ref+`"`) {
		t.Fatalf("fetch destination %s disagrees with modelRef %q", args, ref)
	}
}

func TestPrefetchJobErrors(t *testing.T) {
	cases := []struct {
		name  string
		art   wire.ArtifactDesired
		image string
		want  string
	}{
		{
			name:  "no image",
			art:   hfArtifact(),
			image: "",
			want:  "no image",
		},
		{
			name:  "no sources",
			art:   wire.ArtifactDesired{ArtifactID: "art_empty"},
			image: prefetchImage,
			want:  "no source",
		},
		{
			name:  "blank source uri",
			art:   wire.ArtifactDesired{ArtifactID: "art_blank", Sources: []wire.ArtifactSource{{Kind: "s3"}}},
			image: prefetchImage,
			want:  "no source",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := PrefetchJob(tc.art, tc.image, testOptions())
			if err == nil {
				t.Fatal("expected an error")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not mention %q", err, tc.want)
			}
		})
	}
}

// TestPrefetchJobIsDeterministic is the property server-side apply needs: the same inputs must
// render byte-identically, or every reconcile writes a new object generation for no reason.
func TestPrefetchJobIsDeterministic(t *testing.T) {
	for _, art := range []wire.ArtifactDesired{hfArtifact(), {
		ArtifactID: "art_custom",
		Sources:    []wire.ArtifactSource{{URI: "s3://weights/a"}, {URI: "https://example.invalid/b"}},
	}} {
		first, err := PrefetchJob(art, prefetchImage, testOptions())
		if err != nil {
			t.Fatalf("render: %v", err)
		}
		second, err := PrefetchJob(art, prefetchImage, testOptions())
		if err != nil {
			t.Fatalf("render: %v", err)
		}
		a, err := json.Marshal(first)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		b, err := json.Marshal(second)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		if string(a) != string(b) {
			t.Fatalf("two renders differ:\n%s\n%s", a, b)
		}
	}
}

func TestPrefetchJobName(t *testing.T) {
	cases := []struct {
		id   string
		want string
	}{
		{id: "art_01J9X2", want: "prefetch-art-01j9x2"},
		{id: "ART_ABC", want: "prefetch-art-abc"},
		{id: strings.Repeat("a", 80), want: "prefetch-" + strings.Repeat("a", 54)},
	}
	for _, tc := range cases {
		got := PrefetchJobName(tc.id)
		if got != tc.want {
			t.Fatalf("PrefetchJobName(%q) = %q, want %q", tc.id, got, tc.want)
		}
		if len(got) > 63 {
			t.Fatalf("name %q is %d characters", got, len(got))
		}
		if strings.HasSuffix(got, "-") || strings.ToLower(got) != got {
			t.Fatalf("name %q is not DNS-1123 safe", got)
		}
	}
}
