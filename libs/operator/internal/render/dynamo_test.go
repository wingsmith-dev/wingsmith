package render

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

func testInputs() Inputs {
	return Inputs{
		Deployment: wire.DeploymentDesired{
			DeploymentID: "dep_01J9X2",
			ProjectID:    "prj_01J9X2",
			RecipeID:     "rec_01J9X2",
			MinReplicas:  2,
			MaxReplicas:  8,
			Generation:   7,
			Placement:    wire.PlacementConstraints{DevicesPerReplica: 2},
		},
		Recipe: wire.RecipeDesired{
			RecipeID:           "rec_01J9X2",
			Name:               "qwen3-8b",
			Version:            3,
			Engine:             "vllm",
			EngineImage:        ptr(prefetchImage),
			ContextLength:      8192,
			PrefixCacheEnabled: true,
			Parallelism:        wire.RecipeParallelism{Tensor: 2, Pipeline: 1},
		},
		Artifact: hfArtifact(),
	}
}

func component(t *testing.T, obj *unstructured.Unstructured) map[string]any {
	t.Helper()
	comps, found, err := unstructured.NestedSlice(obj.Object, "spec", "components")
	if err != nil || !found || len(comps) != 1 {
		t.Fatalf("spec.components = %v (found %v, err %v)", comps, found, err)
	}
	c, ok := comps[0].(map[string]any)
	if !ok {
		t.Fatalf("component is %T", comps[0])
	}
	return c
}

func renderedPod(t *testing.T, obj *unstructured.Unstructured) corev1.PodSpec {
	t.Helper()
	raw, found, err := unstructured.NestedMap(component(t, obj), "podTemplate", "spec")
	if err != nil || !found {
		t.Fatalf("podTemplate.spec missing: found %v, err %v", found, err)
	}
	var pod corev1.PodSpec
	if err := runtime.DefaultUnstructuredConverter.FromUnstructured(raw, &pod); err != nil {
		t.Fatalf("decode pod spec: %v", err)
	}
	return pod
}

func TestDynamoGraphDeploymentShape(t *testing.T) {
	obj, err := DynamoGraphDeployment(testInputs(), testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}

	if obj.GroupVersionKind() != GVK {
		t.Fatalf("gvk = %v, want %v", obj.GroupVersionKind(), GVK)
	}
	if obj.GetName() != "dep-01j9x2" {
		t.Fatalf("name = %q", obj.GetName())
	}
	if obj.GetNamespace() != "dynamo" {
		t.Fatalf("namespace = %q", obj.GetNamespace())
	}

	c := component(t, obj)
	// MinReplicas, not MaxReplicas: nothing autoscales, so the ceiling must not be what runs.
	if c["replicas"] != int64(2) {
		t.Fatalf("replicas = %v, want the declared minimum", c["replicas"])
	}
	// Workers join the one shared frontend on the cluster; without this they are unroutable.
	if c["globalDynamoNamespace"] != true {
		t.Fatalf("globalDynamoNamespace = %v", c["globalDynamoNamespace"])
	}
}

func TestDynamoGraphDeploymentPod(t *testing.T) {
	obj, err := DynamoGraphDeployment(testInputs(), testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	pod := renderedPod(t, obj)

	main := pod.Containers[0]
	if v, ok := envValue(main, "PYTHONHASHSEED"); !ok || v != "0" {
		t.Fatalf("PYTHONHASHSEED = %q, %v; unseeded hashing silently loses every prefix cache hit", v, ok)
	}
	if v, ok := envValue(main, "HF_HOME"); !ok || v != "/models/hf" {
		t.Fatalf("HF_HOME = %q, %v", v, ok)
	}
	if got := pod.NodeSelector["hangar.io/pool"]; got != "gpu" {
		t.Fatalf("node selector pool = %q", got)
	}

	var shm *corev1.Volume
	for i := range pod.Volumes {
		if pod.Volumes[i].Name == "dshm" {
			shm = &pod.Volumes[i]
		}
	}
	if shm == nil || shm.EmptyDir == nil {
		t.Fatalf("no dshm emptyDir volume: %+v", pod.Volumes)
	}
	if shm.EmptyDir.Medium != corev1.StorageMediumMemory {
		t.Fatalf("dshm medium = %q, want Memory", shm.EmptyDir.Medium)
	}
}

func TestDynamoGraphDeploymentEngineArgs(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*Inputs)
		want   []string
		absent []string
	}{
		{
			name:   "tensor parallel above one is rendered",
			mutate: func(in *Inputs) { in.Recipe.Parallelism.Tensor = 4 },
			want:   []string{"--tensor-parallel-size", "4"},
		},
		{
			name:   "tensor parallel of one is omitted",
			mutate: func(in *Inputs) { in.Recipe.Parallelism.Tensor = 1 },
			absent: []string{"--tensor-parallel-size"},
		},
		{
			name:   "prefix caching off is stated",
			mutate: func(in *Inputs) { in.Recipe.PrefixCacheEnabled = false },
			want:   []string{"--no-enable-prefix-caching"},
		},
		{
			// engine_args is the escape hatch for a flag this renderer has never heard
			// of, and an escape hatch that loses to a derived default is not one.
			name: "engine args override a derived arg",
			mutate: func(in *Inputs) {
				in.Recipe.EngineArgs = map[string]string{"--max-model-len": "4096"}
			},
			want:   []string{"--max-model-len", "4096"},
			absent: []string{"8192"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			in := testInputs()
			tc.mutate(&in)
			obj, err := DynamoGraphDeployment(in, testOptions())
			if err != nil {
				t.Fatalf("render: %v", err)
			}
			args := strings.Join(renderedPod(t, obj).Containers[0].Args, " ")
			for _, want := range tc.want {
				if !strings.Contains(args, want) {
					t.Fatalf("args %q do not contain %q", args, want)
				}
			}
			for _, absent := range tc.absent {
				if strings.Contains(args, absent) {
					t.Fatalf("args %q still contain %q", args, absent)
				}
			}
		})
	}
}

// TestPodOverlayMergesByName is the behaviour that justifies strategic merge over RFC 7386. Under a
// plain JSON merge patch the env list is replaced wholesale, so adding one variable would drop
// PYTHONHASHSEED and cost every prefix cache hit without reporting anything.
func TestPodOverlayMergesByName(t *testing.T) {
	in := testInputs()
	in.Recipe.Overlay.PodSpec = map[string]any{
		"containers": []any{map[string]any{
			"name": "main",
			"env":  []any{map[string]any{"name": "VLLM_LOGGING_LEVEL", "value": "DEBUG"}},
		}},
	}
	obj, err := DynamoGraphDeployment(in, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	main := renderedPod(t, obj).Containers[0]
	if v, ok := envValue(main, "VLLM_LOGGING_LEVEL"); !ok || v != "DEBUG" {
		t.Fatalf("overlay env not merged: %q, %v", v, ok)
	}
	if v, ok := envValue(main, "PYTHONHASHSEED"); !ok || v != "0" {
		t.Fatalf("overlay dropped PYTHONHASHSEED: %q, %v", v, ok)
	}
	if v, ok := envValue(main, "HF_HOME"); !ok || v != "/models/hf" {
		t.Fatalf("overlay dropped HF_HOME: %q, %v", v, ok)
	}
	if main.Image != prefetchImage {
		t.Fatalf("overlay dropped the image: %q", main.Image)
	}
}

// TestDynamoOverlayReplacesComponents is the other half of the overlay story: there is no schema to
// read merge keys off a foreign CRD, so lists replace, and replacing spec.components wholesale is
// the intended way to hand-write a topology this renderer cannot derive.
func TestDynamoOverlayReplacesComponents(t *testing.T) {
	in := testInputs()
	in.Recipe.Overlay.Dynamo = map[string]any{
		"components": []any{
			map[string]any{"name": "VllmPrefillWorker", "replicas": int64(1)},
			map[string]any{"name": "VllmDecodeWorker", "replicas": int64(3)},
		},
	}
	obj, err := DynamoGraphDeployment(in, testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	comps, _, err := unstructured.NestedSlice(obj.Object, "spec", "components")
	if err != nil {
		t.Fatalf("components: %v", err)
	}
	if len(comps) != 2 {
		t.Fatalf("components = %v, want the overlay's two", comps)
	}
	first, _ := comps[0].(map[string]any)
	if first["name"] != "VllmPrefillWorker" {
		t.Fatalf("components were merged rather than replaced: %v", comps)
	}
}

func TestDynamoGraphDeploymentRequiresEngineImage(t *testing.T) {
	for _, image := range []*string{nil, ptr("")} {
		in := testInputs()
		in.Recipe.EngineImage = image
		if _, err := DynamoGraphDeployment(in, testOptions()); err == nil {
			t.Fatalf("expected an error for engine image %v", image)
		} else if !strings.Contains(err.Error(), "engine image") {
			t.Fatalf("error %q does not name the engine image", err)
		}
	}
}
