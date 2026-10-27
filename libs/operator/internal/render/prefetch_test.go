package render

import (
	"encoding/json"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

const prefetchImage = "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1"

func testOptions() Options {
	return Options{
		Namespace:      "dynamo",
		PoolLabelKey:   "hangar.dev/pool",
		GPUPoolValue:   "gpu",
		CachePoolValue: "cache",
		CacheTaintKey:  "hangar.dev/cache",
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
	if job.Name != "qwen3-8b-prefetch-art-01j9x2aaaaaaaaaaaaaaaaaaaa" {
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
	if got := pod.NodeSelector["hangar.dev/pool"]; got != "cache" {
		t.Fatalf("node selector pool = %q, want the cache pool", got)
	}
	if len(pod.Tolerations) != 1 || pod.Tolerations[0].Key != "hangar.dev/cache" {
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
	if !strings.Contains(c.Args[0], "max_workers=4") {
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
// are a first-class case, so it must render a job, not refuse, and it must land them at the
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

	ref, _, err := modelRef(art)
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

// sourced is an artifact with one source and nothing else, for the cases that only care what the
// name and the labels read off it.
func sourced(id, uri string) wire.ArtifactDesired {
	return wire.ArtifactDesired{ArtifactID: id, Sources: []wire.ArtifactSource{{URI: uri}}}
}

// TestAPrefetchJobNameLeadsWithTheModel is what the shape is for: a `kubectl get jobs` in
// the workload namespace has to read as who is downloading, not as a column of opaque ids.
func TestAPrefetchJobNameLeadsWithTheModel(t *testing.T) {
	cases := []struct {
		art  wire.ArtifactDesired
		want string
	}{
		{sourced("art_01J9X2", "hf://moonshotai/Kimi-K3"), "kimi-k3-prefetch-art-01j9x2"},
		{sourced("ART_ABC", "hf://Qwen/Qwen3-8B@main"), "qwen3-8b-prefetch-art-abc"},
		{sourced("art_01J9X2", "s3://weights/custom/llama.safetensors"), "llama-safetensors-prefetch-art-01j9x2"},
	}
	for _, tc := range cases {
		if got := PrefetchJobName(tc.art); got != tc.want {
			t.Errorf("PrefetchJobName(%s) = %q, want %q", tc.art.Sources[0].URI, got, tc.want)
		}
	}
}

// TestAnArtifactWithNoReadableSourceStillGetsAName. The model is decoration over the id, so its
// absence must fall back to the older shape, not render a placeholder segment or nothing.
func TestAnArtifactWithNoReadableSourceStillGetsAName(t *testing.T) {
	for _, art := range []wire.ArtifactDesired{
		{ArtifactID: "art_01J9X2"},
		{ArtifactID: "art_01J9X2", Sources: []wire.ArtifactSource{{Kind: "s3"}}},
		{ArtifactID: "art_01J9X2", Sources: []wire.ArtifactSource{{URI: "s3://weights/---"}}},
	} {
		if got := PrefetchJobName(art); got != "prefetch-art-01j9x2" {
			t.Errorf("name = %q, want the bare shape when no model can be read", got)
		}
	}
}

// TestTheArtifactIDSurvivesTruncation is the property that makes the name safe to change at all:
// the id is what distinguishes one job from another, and the model is only for the reader, so a
// long model must lose characters and a long id must never lose them to it.
func TestTheArtifactIDSurvivesTruncation(t *testing.T) {
	long := sourced("art_01J9X2AAAAAAAAAAAAAAAAAAAA", "hf://acme/"+strings.Repeat("verylongmodel", 6))
	got := PrefetchJobName(long)
	if !strings.HasSuffix(got, "prefetch-art-01j9x2aaaaaaaaaaaaaaaaaaaa") {
		t.Errorf("name = %q, want the whole artifact id intact", got)
	}
	if !strings.HasPrefix(got, "verylongmodel") {
		t.Errorf("name = %q, want what fits of the model in front of it", got)
	}

	// An id that fills the limit on its own leaves no room to say anything about the model.
	if got := PrefetchJobName(sourced(strings.Repeat("a", 80), "hf://acme/kimi")); got != "prefetch-"+strings.Repeat("a", 54) {
		t.Errorf("name = %q, want the id alone", got)
	}
}

func TestEveryPrefetchJobNameIsDNS1123Safe(t *testing.T) {
	for _, art := range []wire.ArtifactDesired{
		hfArtifact(),
		sourced("art_01J9X2", "hf://acme/"+strings.Repeat("verylongmodel", 6)),
		sourced(strings.Repeat("a", 80), "hf://acme/kimi"),
		sourced("art_01J9X2", "s3://weights/.hidden."),
		{ArtifactID: "art_01J9X2"},
	} {
		got := PrefetchJobName(art)
		if len(got) > 63 {
			t.Errorf("name %q is %d characters", got, len(got))
		}
		if strings.ToLower(got) != got {
			t.Errorf("name %q is not lower case", got)
		}
		if strings.HasPrefix(got, "-") || strings.HasSuffix(got, "-") {
			t.Errorf("name %q begins or ends on a separator", got)
		}
	}
}

// TestAPrefetchJobSaysWhichModelItIsPulling covers the reader the name cannot always serve: a
// truncated slug still leaves the label whole, and the annotation still holds the exact source.
func TestAPrefetchJobSaysWhichModelItIsPulling(t *testing.T) {
	art := hfArtifact()
	job, err := PrefetchJob(art, prefetchImage, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}

	for what, meta := range map[string]metav1.ObjectMeta{
		"job": job.ObjectMeta, "pod": job.Spec.Template.ObjectMeta,
	} {
		if got := meta.Labels[ModelLabel]; got != "qwen3-8b" {
			t.Errorf("%s model label = %q", what, got)
		}
		if got := meta.Annotations[ModelSourceAnnotation]; got != "hf://Qwen/Qwen3-8B" {
			t.Errorf("%s model source annotation = %q", what, got)
		}
	}

	// The Job's labels take the warm-set's origin and the pod template's must not, or the two
	// would be one map and an immutable pod selector would move with it.
	OwnedMeta(job, &metav1.ObjectMeta{Name: "cch-1", Namespace: "hangar-system"})
	if _, ok := job.Spec.Template.Labels[OwnerNameLabel]; ok {
		t.Error("the owner label leaked into the pod template; job and pod share one label map")
	}
}

// TestAnArtifactWithNoModelIsNotLabelledWithNothing: an empty label value is a legal label and a
// misleading one, reading as a model named "", not as a model nobody could name.
func TestAnArtifactWithNoModelIsNotLabelledWithNothing(t *testing.T) {
	job, err := PrefetchJob(wire.ArtifactDesired{
		ArtifactID: "art_01J9X2",
		Sources:    []wire.ArtifactSource{{URI: "s3://weights/---"}},
	}, prefetchImage, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if _, ok := job.Labels[ModelLabel]; ok {
		t.Errorf("model label = %q, want no label at all", job.Labels[ModelLabel])
	}
	if got := job.Annotations[ModelSourceAnnotation]; got != "s3://weights/---" {
		t.Errorf("source annotation = %q, want the URI the slug could not be read from", got)
	}
}

// TestARunningPrefetchSaysHowFarItHasGot. The bars stay off (they bury the one line worth reading),
// but a job that prints nothing for forty minutes cannot be told from a stuck one.
func TestARunningPrefetchSaysHowFarItHasGot(t *testing.T) {
	for _, art := range []wire.ArtifactDesired{hfArtifact(), sourced("art_custom", "s3://weights/custom/model.bin")} {
		job, err := PrefetchJob(art, prefetchImage, testOptions())
		if err != nil {
			t.Fatalf("render: %v", err)
		}
		args := job.Spec.Template.Spec.Containers[0].Args[0]
		if !strings.Contains(args, "prefetch progress bytes=") {
			t.Errorf("the fetch prints no progress at all:\n%s", args)
		}
		if !strings.Contains(args, "elapsed_seconds=") {
			t.Errorf("progress carries no elapsed time to read a stall against:\n%s", args)
		}
		if !strings.Contains(args, "daemon=True") {
			t.Errorf("the progress thread could hold the job open past the fetch:\n%s", args)
		}
		if v, ok := envValue(job.Spec.Template.Spec.Containers[0], "HF_HUB_DISABLE_PROGRESS_BARS"); !ok || v != "1" {
			t.Errorf("progress bars were reinstated: %q, %v", v, ok)
		}
	}
}

// TestProgressIsCountedOnlyUnderTheModelBeingFetched: HF_HOME is the shared volume every artifact
// lands in, so a walk over its root would report each concurrent job the other's bytes.
func TestProgressIsCountedOnlyUnderTheModelBeingFetched(t *testing.T) {
	command, err := prefetchCommand(hfArtifact())
	if err != nil {
		t.Fatalf("command: %v", err)
	}
	if !strings.Contains(command, `"models--" + repo.replace("/", "--")`) {
		t.Errorf("progress is not scoped to this artifact's own cache directory:\n%s", command)
	}
}

func TestSlug(t *testing.T) {
	for _, c := range []struct{ in, want string }{
		{"Kimi-K3", "kimi-k3"},
		{"Llama-3.1-8B-Instruct.safetensors", "llama-3-1-8b-instruct-safetensors"},
		{"..weird..name..", "weird-name"},
		{"----", ""},
	} {
		if got := slug(c.in); got != c.want {
			t.Errorf("slug(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// The figures this replaced (four CPUs and eight gigabytes) were larger than an m7i.large has
// allocatable at all, so the job could not be placed on a system node however many were added.
func TestAPrefetchAsksForLittleEnoughToBePlaced(t *testing.T) {
	job, err := PrefetchJob(hfArtifact(), "runtime:latest", Options{Namespace: "dynamo"})
	if err != nil {
		t.Fatalf("render: %v", err)
	}

	req := job.Spec.Template.Spec.Containers[0].Resources.Requests
	if got := req.Cpu().String(); got != "500m" {
		t.Errorf("cpu request = %q, want 500m", got)
	}
	if got := req.Memory().String(); got != "2Gi" {
		t.Errorf("memory request = %q, want 2Gi", got)
	}
	if got := job.Spec.Template.Spec.Containers[0].Resources.Limits.Memory().String(); got != "6Gi" {
		t.Errorf("memory limit = %q, want the ceiling in its own right", got)
	}
}

// A cluster with a dedicated cache pool should be able to spend more on the download.
func TestAClusterCanRaiseWhatAPrefetchAsksFor(t *testing.T) {
	job, err := PrefetchJob(hfArtifact(), "runtime:latest", Options{
		Namespace: "dynamo", PrefetchCPU: "4", PrefetchMemory: "8Gi", PrefetchMemoryLimit: "16Gi",
	})
	if err != nil {
		t.Fatalf("render: %v", err)
	}

	req := job.Spec.Template.Spec.Containers[0].Resources.Requests
	if got := req.Cpu().String(); got != "4" {
		t.Errorf("cpu request = %q, want the cluster's own", got)
	}
	if got := req.Memory().String(); got != "8Gi" {
		t.Errorf("memory request = %q, want the cluster's own", got)
	}
}

func TestAnUnparseableQuantityIsRefusedAtRender(t *testing.T) {
	if _, err := PrefetchJob(hfArtifact(), "runtime:latest", Options{PrefetchCPU: "a lot"}); err == nil {
		t.Error("a quantity the scheduler cannot read must fail the render, not the pod")
	}
}

// The plane pins an artifact as hf://owner/name@<sha>, and huggingface_hub rejects an '@' in a repo
// id outright, so the whole URI as a repo id is a failed pull, not a resolved pin.
func TestAPinnedRevisionIsNotPartOfTheRepoID(t *testing.T) {
	art := wire.ArtifactDesired{
		ArtifactID: "art_01J9X2AAAAAAAAAAAAAAAAAAAA",
		Sources: []wire.ArtifactSource{
			{Kind: "hf", URI: "hf://openai/gpt-oss-20b@6cee5e81ee83917806bbde320786a8fb61efebee"},
		},
	}

	command, err := prefetchCommand(art)
	if err != nil {
		t.Fatalf("command: %v", err)
	}
	if !strings.Contains(command, `"openai/gpt-oss-20b"`) {
		t.Errorf("repo id still carries the pin:\n%s", command)
	}
	if !strings.Contains(command, `"6cee5e81ee83917806bbde320786a8fb61efebee"`) {
		t.Errorf("the pin is dropped, not resolved:\n%s", command)
	}
}

func TestAnUnpinnedSourceDownloadsTheBranchHead(t *testing.T) {
	command, err := prefetchCommand(hfArtifact())
	if err != nil {
		t.Fatalf("command: %v", err)
	}
	if !strings.Contains(command, `"" or None`) {
		t.Errorf("an unpinned source must resolve normally:\n%s", command)
	}
}

func TestSplitHFRef(t *testing.T) {
	for _, c := range []struct{ uri, repo, revision string }{
		{"hf://Qwen/Qwen3-8B", "Qwen/Qwen3-8B", ""},
		{"hf://Qwen/Qwen3-8B@main", "Qwen/Qwen3-8B", "main"},
		{"hf://openai/gpt-oss-20b@6cee5e8", "openai/gpt-oss-20b", "6cee5e8"},
	} {
		repo, revision := SplitHFRef(c.uri)
		if repo != c.repo || revision != c.revision {
			t.Errorf("SplitHFRef(%q) = %q, %q; want %q, %q", c.uri, repo, revision, c.repo, c.revision)
		}
	}
}

// The ceiling must not follow the request down. It did, and a large model was OOM killed part-way
// through: pages written to the shared NFS volume are charged to the cgroup until EFS writeback
// takes them, and that backlog is far larger than anything the job means to hold.
func TestTheMemoryCeilingIsNotDerivedFromTheRequest(t *testing.T) {
	job, err := PrefetchJob(hfArtifact(), "runtime:latest", Options{PrefetchMemory: "1Gi"})
	if err != nil {
		t.Fatalf("render: %v", err)
	}

	limit := job.Spec.Template.Spec.Containers[0].Resources.Limits.Memory()
	if limit.String() != "6Gi" {
		t.Errorf("memory limit = %q, want the default ceiling regardless of the request", limit)
	}
}

func TestACeilingBelowTheRequestIsRefused(t *testing.T) {
	_, err := PrefetchJob(hfArtifact(), "runtime:latest", Options{
		PrefetchMemory: "8Gi", PrefetchMemoryLimit: "2Gi",
	})
	if err == nil {
		t.Error("a ceiling under the request is unschedulable and must fail the render")
	}
}

// The download runs the engine image, so a private engine registry has to be reachable from the
// prefetch job too. Without this the pull fails while the credential sits unused in the same
// namespace, and nothing in the event says which of the two is missing.
func TestThePrefetchJobTakesTheEnginePullSecrets(t *testing.T) {
	opts := testOptions()
	opts.EnginePullSecrets = []string{"ghcr-pull", "ghcr-pull"}

	job, err := PrefetchJob(hfArtifact(), prefetchImage, opts)
	if err != nil {
		t.Fatalf("PrefetchJob: %v", err)
	}

	secrets := job.Spec.Template.Spec.ImagePullSecrets
	if len(secrets) != 1 || secrets[0].Name != "ghcr-pull" {
		t.Fatalf("imagePullSecrets = %v", secrets)
	}

	bare, err := PrefetchJob(hfArtifact(), prefetchImage, testOptions())
	if err != nil {
		t.Fatalf("PrefetchJob: %v", err)
	}
	if len(bare.Spec.Template.Spec.ImagePullSecrets) != 0 {
		t.Fatalf("imagePullSecrets = %v with none configured", bare.Spec.Template.Spec.ImagePullSecrets)
	}
}

// The cache taint is tolerated whatever value the cluster gave it, or exactly the configured one,
// and an unset key tolerates nothing instead of everything.
func TestPrefetchToleratesTheCacheTaintByKey(t *testing.T) {
	opts := testOptions()
	job, err := PrefetchJob(hfArtifact(), prefetchImage, opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	tol := job.Spec.Template.Spec.Tolerations
	if len(tol) != 1 || tol[0].Operator != corev1.TolerationOpExists || tol[0].Value != "" {
		t.Errorf("tolerations = %+v, want Exists on the cache key", tol)
	}

	opts.CacheTaintValue = "dedicated"
	job, err = PrefetchJob(hfArtifact(), prefetchImage, opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	tol = job.Spec.Template.Spec.Tolerations
	if len(tol) != 1 || tol[0].Operator != corev1.TolerationOpEqual || tol[0].Value != "dedicated" {
		t.Errorf("tolerations = %+v, want Equal on the configured value", tol)
	}

	opts.CacheTaintKey = ""
	job, err = PrefetchJob(hfArtifact(), prefetchImage, opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if tol := job.Spec.Template.Spec.Tolerations; len(tol) != 0 {
		t.Errorf("tolerations = %+v, want none without a key", tol)
	}
}

func TestPrefetchRunsWithoutEscalationAsTheImagesUser(t *testing.T) {
	job, err := PrefetchJob(hfArtifact(), prefetchImage, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	spec := job.Spec.Template.Spec
	if spec.AutomountServiceAccountToken == nil || *spec.AutomountServiceAccountToken {
		t.Error("the prefetch job mounts a service-account token")
	}
	// The image's own user, because the shared cache is owned by whoever wrote it first and
	// fsGroup does not re-own a network volume.
	if sc := spec.SecurityContext; sc == nil || sc.RunAsUser != nil || sc.RunAsNonRoot != nil || sc.FSGroup != nil || sc.SeccompProfile == nil {
		t.Errorf("pod security context = %+v, want the image's user with a seccomp profile", sc)
	}
	if csc := spec.Containers[0].SecurityContext; csc == nil || csc.AllowPrivilegeEscalation == nil || *csc.AllowPrivilegeEscalation {
		t.Errorf("container security context = %+v, want no privilege escalation", csc)
	}
}
