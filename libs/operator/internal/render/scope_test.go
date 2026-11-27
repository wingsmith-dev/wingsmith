package render

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

const testProject = "prj_01j9x2pppppppppppppppppppp"

func privateArtifact() wire.ArtifactDesired {
	art := hfArtifact()
	art.OwnerProject = testProject
	ref := "acme-hf"
	art.Sources = []wire.ArtifactSource{{Kind: "hf", URI: "hf://acme/secret-model@0123abcd", Priority: 0, CredentialRef: &ref}}
	return art
}

func secretNames(from []corev1.EnvFromSource) []string {
	var names []string
	for _, f := range from {
		if f.SecretRef != nil {
			names = append(names, f.SecretRef.Name)
		}
	}
	return names
}

func TestProjectTokenSecretName(t *testing.T) {
	got := ProjectTokenSecret("dynamo", testProject)
	if got.Namespace != "dynamo" || got.Name != "hf-prj-01j9x2pppppppppppppppppppp" {
		t.Fatalf("secret = %v", got)
	}
	if other := ProjectTokenSecret("tenant-b", testProject); other.Namespace != "tenant-b" {
		t.Fatalf("namespace is not carried: %v", other)
	}
}

// The prefetch job is the only pod that holds a private artifact's token, and it writes under the
// project's directory, never the volume root.
func TestPrivatePrefetchJobUsesTheProjectTokenAndDirectory(t *testing.T) {
	job, err := PrefetchJob(privateArtifact(), prefetchImage, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	c := job.Spec.Template.Spec.Containers[0]
	if names := secretNames(c.EnvFrom); len(names) != 1 || names[0] != "hf-prj-01j9x2pppppppppppppppppppp" {
		t.Fatalf("envFrom secrets = %v, want only the project token", names)
	}
	if c.EnvFrom[0].SecretRef.Optional != nil && *c.EnvFrom[0].SecretRef.Optional {
		t.Fatalf("a private fetch must not run anonymously when its token is missing")
	}
	if v, _ := envValue(c, "HF_HOME"); v != "/models/p/"+testProject+"/hf" {
		t.Fatalf("HF_HOME = %q", v)
	}
	if len(c.VolumeMounts) != 1 || c.VolumeMounts[0].SubPath != "p/"+testProject ||
		c.VolumeMounts[0].MountPath != "/models/p/"+testProject {
		t.Fatalf("mounts = %+v", c.VolumeMounts)
	}
}

// A private artifact with no credential fetches anonymously, and never with the cluster's token.
func TestPrivatePrefetchJobNeverBorrowsTheClusterToken(t *testing.T) {
	art := privateArtifact()
	art.Sources[0].CredentialRef = nil
	job, err := PrefetchJob(art, prefetchImage, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if names := secretNames(job.Spec.Template.Spec.Containers[0].EnvFrom); len(names) != 0 {
		t.Fatalf("envFrom secrets = %v, want none", names)
	}
}

func TestPublicPrefetchJobKeepsTheClusterToken(t *testing.T) {
	job, err := PrefetchJob(hfArtifact(), prefetchImage, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	from := job.Spec.Template.Spec.Containers[0].EnvFrom
	if names := secretNames(from); len(names) != 1 || names[0] != "hf-token-secret" || !*from[0].SecretRef.Optional {
		t.Fatalf("envFrom = %+v", from)
	}
}

func TestPrivateCustomWeightsLandUnderTheProject(t *testing.T) {
	art := wire.ArtifactDesired{
		ArtifactID:   "art_01j9x2bbbbbbbbbbbbbbbbbbbb",
		OwnerProject: testProject,
		Sources:      []wire.ArtifactSource{{Kind: "s3", URI: "s3://acme/weights.safetensors"}},
	}
	job, err := PrefetchJob(art, prefetchImage, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	c := job.Spec.Template.Spec.Containers[0]
	if !strings.Contains(c.Args[0], "/models/p/"+testProject+"/art_01j9x2bbbbbbbbbbbbbbbbbbbb") {
		t.Fatalf("fetch does not write under the project: %s", c.Args[0])
	}
	if c.VolumeMounts[0].SubPath != "p/"+testProject {
		t.Fatalf("mounts = %+v", c.VolumeMounts)
	}
	ref, isPath, err := modelRef(art)
	if err != nil || !isPath || ref != "/models/p/"+testProject+"/art_01j9x2bbbbbbbbbbbbbbbbbbbb" {
		t.Fatalf("modelRef = %q %v %v", ref, isPath, err)
	}
}

func TestPrefetchRefusesAProjectIDThatIsNotOne(t *testing.T) {
	art := privateArtifact()
	art.OwnerProject = "../other"
	if _, err := PrefetchJob(art, prefetchImage, testOptions()); err == nil {
		t.Fatal("rendered a job for a project id that walks out of the cache")
	}
}

func TestRefusesAnArtifactIDThatIsNotOne(t *testing.T) {
	for _, id := range []string{"../projects/other", "..", "a/b", "art_x/../../p/other"} {
		for _, owner := range []string{"", testProject} {
			art := wire.ArtifactDesired{
				ArtifactID:   id,
				OwnerProject: owner,
				Sources:      []wire.ArtifactSource{{Kind: "s3", URI: "s3://acme/weights.safetensors"}},
			}
			if _, err := PrefetchJob(art, prefetchImage, testOptions()); err == nil {
				t.Errorf("prefetch rendered for artifact id %q (owner %q)", id, owner)
			}
			if _, _, err := modelRef(art); err == nil {
				t.Errorf("modelRef accepted artifact id %q (owner %q)", id, owner)
			}
			if _, err := modelCacheMount(art, true); err == nil {
				t.Errorf("mount accepted artifact id %q (owner %q)", id, owner)
			}
		}
	}
}

// Serving pods of a private artifact read the project's cache offline, with no token and no
// mount of the volume root.
func TestPrivateServingPodsMountOnlyTheirProjectAndHoldNoToken(t *testing.T) {
	in := testInputs()
	in.Artifact = privateArtifact()
	opts := frontendOptions()
	opts.ModelCachePVC = "model-cache"
	opts.LocalModelCacheDir = "/var/lib/wingsmith/models"
	obj, err := DynamoGraphDeployment(in, opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}

	worker := componentPod(t, componentNamed(t, obj, WorkerComponent))
	frontend := componentPod(t, componentNamed(t, obj, FrontendComponent))
	containers := append(append(append([]corev1.Container{}, worker.Containers...), worker.InitContainers...),
		append(frontend.Containers, frontend.InitContainers...)...)
	for _, c := range containers {
		if names := secretNames(c.EnvFrom); len(names) != 0 {
			t.Fatalf("container %s reads secrets %v", c.Name, names)
		}
		for _, m := range c.VolumeMounts {
			if m.Name != "model-cache" {
				continue
			}
			if m.SubPath != "p/"+testProject || m.MountPath != "/models/p/"+testProject {
				t.Fatalf("container %s mounts the cache as %+v", c.Name, m)
			}
		}
	}

	main := worker.Containers[0]
	if v, _ := envValue(main, "HF_HOME"); v != "/models/p/"+testProject+"/hf" {
		t.Fatalf("HF_HOME = %q", v)
	}
	if v, _ := envValue(main, "HF_HUB_OFFLINE"); v != "1" {
		t.Fatalf("HF_HUB_OFFLINE = %q", v)
	}
	want := "/models/p/" + testProject + "/hf/hub/models--acme--secret-model/snapshots/0123abcd"
	ref, _, _ := modelRef(in.Artifact)
	if ref != want {
		t.Fatalf("modelRef = %q, want %q", ref, want)
	}
}

func TestPublicServingPodsMountOnlyThePublicCache(t *testing.T) {
	opts := frontendOptions()
	opts.ModelCachePVC = "model-cache"
	obj, err := DynamoGraphDeployment(testInputs(), opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	worker := componentPod(t, componentNamed(t, obj, WorkerComponent))
	main := worker.Containers[0]
	if names := secretNames(main.EnvFrom); len(names) != 1 || names[0] != "hf-token-secret" {
		t.Fatalf("envFrom = %v", names)
	}
	for _, m := range main.VolumeMounts {
		if m.Name == "model-cache" && (m.SubPath != "hf" || m.MountPath != "/models/hf") {
			t.Fatalf("mount = %+v", m)
		}
	}
	if _, ok := envValue(main, "HF_HUB_OFFLINE"); ok {
		t.Fatal("a public worker was forced offline")
	}
}

func TestBenchmarkMountsThePublicCacheAndItsProjectOnly(t *testing.T) {
	public := mustBenchmarkJob(t, benchParams(nil)).Spec.Template.Spec.Containers[0]
	for _, m := range public.VolumeMounts {
		if m.Name == "model-cache" && (m.SubPath != "hf" || !m.ReadOnly) {
			t.Fatalf("public mount = %+v", m)
		}
	}

	scoped := mustBenchmarkJob(t, benchParams(map[string]string{
		"cache_project": testProject, "tokenizer": "acme/secret-model",
	})).Spec.Template.Spec.Containers[0]
	var subs []string
	for _, m := range scoped.VolumeMounts {
		if m.Name == "model-cache" {
			subs = append(subs, m.SubPath)
		}
	}
	if strings.Join(subs, ",") != "hf,p/"+testProject {
		t.Fatalf("cache subPaths = %v", subs)
	}
	if v, _ := envValue(scoped, "HF_HOME"); v != "/models/p/"+testProject+"/hf" {
		t.Fatalf("HF_HOME = %q", v)
	}
	if !strings.Contains(scoped.Args[0], "/models/p/"+testProject+"/hf/hub/models--acme--secret-model") {
		t.Fatalf("tokenizer is not looked up in the project cache: %s", scoped.Args[0])
	}

	opts, bench := benchOpts()
	if _, err := BenchmarkJob("bmk_x", benchParams(map[string]string{"cache_project": "../x"}), opts, bench); err == nil {
		t.Fatal("rendered a benchmark mounting a path outside the cache")
	}
}
