package render

import (
	"encoding/json"
	"reflect"
	"slices"
	"sort"
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
			DeploymentID:    "dep_01J9X2",
			Name:            "chat-iad",
			ProjectID:       "prj_01J9X2",
			MinReplicas:     2,
			MaxReplicas:     8,
			Generation:      7,
			ServedModelName: "qwen3-8b",
			Placement:       wire.PlacementConstraints{DevicesPerReplica: 2},
			Serving: wire.ServingSpec{
				Engine:             "vllm",
				EngineRuntimeImage: ptr(prefetchImage),
				ContextLength:      8192,
				PrefixCacheEnabled: true,
				Parallelism:        wire.Parallelism{Tensor: 2, Pipeline: 1},
				Digest:             "svg_" + strings.Repeat("a", 64),
			},
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
	// The deployment's name, not its id: everything Dynamo derives is named after this one,
	// so an id here reaches all the way down to the pods.
	if obj.GetName() != "chat-iad" {
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
			mutate: func(in *Inputs) { in.Deployment.Serving.Parallelism.Tensor = 4 },
			want:   []string{"--tensor-parallel-size", "4"},
		},
		{
			name:   "tensor parallel of one is omitted",
			mutate: func(in *Inputs) { in.Deployment.Serving.Parallelism.Tensor = 1 },
			absent: []string{"--tensor-parallel-size"},
		},
		{
			name:   "prefix caching off is stated",
			mutate: func(in *Inputs) { in.Deployment.Serving.PrefixCacheEnabled = false },
			want:   []string{"--no-enable-prefix-caching"},
		},
		{
			// engine_args is the escape hatch for a flag this renderer has never heard
			// of, and an escape hatch that loses to a derived default is not one.
			name: "engine args override a derived arg",
			mutate: func(in *Inputs) {
				in.Deployment.Serving.EngineArgs = map[string]string{"--max-model-len": "4096"}
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
	in.Deployment.Serving.Overlay.PodSpec = map[string]any{
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
	in.Deployment.Serving.Overlay.Dynamo = map[string]any{
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

func TestDynamoGraphDeploymentRequiresEngineRuntimeImage(t *testing.T) {
	for _, image := range []*string{nil, ptr("")} {
		in := testInputs()
		in.Deployment.Serving.EngineRuntimeImage = image
		if _, err := DynamoGraphDeployment(in, testOptions()); err == nil {
			t.Fatalf("expected an error for engine runtime image %v", image)
		} else if !strings.Contains(err.Error(), "engine runtime image") {
			t.Fatalf("error %q does not name the engine runtime image", err)
		}
	}
}

// A plane older than the engine_version -> engine_runtime_version rename sends the two values
// under their old names. Reading only the new keys would leave the deployment with no image, and
// that failure is silent on the cluster: no prefetch job is rendered and the pods wait forever on
// a download nobody started. So the old spelling is still read, and read only as a fallback.
func TestAPlaneThatPredatesTheRenameStillNamesAnImage(t *testing.T) {
	in := testInputs()
	legacy := "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1"
	in.Deployment.Serving.EngineRuntimeImage = nil
	in.Deployment.Serving.EngineRuntimeVersion = ""
	in.Deployment.Serving.LegacyEngineImage = &legacy
	in.Deployment.Serving.LegacyEngineVersion = "1.3.1"

	if got := in.Deployment.Serving.RuntimeVersion(); got != "1.3.1" {
		t.Fatalf("runtime version = %q, want the legacy value", got)
	}
	if _, err := DynamoGraphDeployment(in, testOptions()); err != nil {
		t.Fatalf("legacy image should render: %v", err)
	}

	// And the new spelling wins whenever both are present, so a plane that sends both -- which
	// every plane does for one release -- is read as the new one.
	current := "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.1"
	in.Deployment.Serving.EngineRuntimeImage = &current
	if got := in.Deployment.Serving.RuntimeImage(); got == nil || *got != current {
		t.Fatalf("runtime image = %v, want the new spelling to win", got)
	}
}

// The engine must resolve the same commit the prefetch downloaded, or it re-resolves the branch
// head against the shared cache and pulls a second copy of the weights. A pinned source says so by
// naming the snapshot directory itself, which is the one form every engine reads the same way.
func TestTheEngineIsPointedAtThePinnedRevision(t *testing.T) {
	in := testInputs()
	in.Artifact.Sources = []wire.ArtifactSource{
		{Kind: "hf", URI: "hf://openai/gpt-oss-20b@6cee5e81ee83917806bbde320786a8fb61efebee"},
	}

	args, err := engineArgs(in, Options{}, workerRole{})
	if err != nil {
		t.Fatalf("args: %v", err)
	}
	joined := strings.Join(args, " ")
	want := "--model /models/hf/hub/models--openai--gpt-oss-20b/snapshots/6cee5e81ee83917806bbde320786a8fb61efebee"
	if !strings.Contains(joined, want) {
		t.Errorf("--model is not the snapshot the prefetch wrote: %s", joined)
	}
	if strings.Contains(joined, "@6cee5e") {
		t.Errorf("the pin leaked into the model id: %s", joined)
	}
	// The path has already resolved the revision — its last segment is the commit — so sending
	// --revision as well says the same thing twice and lets an engine disagree with itself.
	if strings.Contains(joined, "--revision") {
		t.Errorf("--revision alongside a resolved path: %s", joined)
	}
}

// An unpinned source has no snapshot directory to name: huggingface_hub resolves the branch head to
// a commit at download time and this renderer cannot know which. It keeps the repo id.
func TestAnUnpinnedSourceKeepsTheRepoID(t *testing.T) {
	in := testInputs()
	in.Artifact.Sources = []wire.ArtifactSource{{Kind: "hf", URI: "hf://openai/gpt-oss-20b"}}

	args, err := engineArgs(in, Options{}, workerRole{})
	if err != nil {
		t.Fatalf("args: %v", err)
	}
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "--model openai/gpt-oss-20b") {
		t.Errorf("--model is not the repo id: %s", joined)
	}
	if strings.Contains(joined, "/snapshots/") {
		t.Errorf("a snapshot path was invented for an unpinned source: %s", joined)
	}
}

// The regression this shape exists for. Z-AI's GLM-5.3-Flash processor opens
// `<--model>/processor_config.json` itself rather than resolving the repo id through
// huggingface_hub, so a repo id reaches it as a relative path and the worker dies on
// `FileNotFoundError: 'zai-org/GLM-5.3-Flash/processor_config.json'`.
func TestAPinnedModelIsServedFromADirectory(t *testing.T) {
	in := testInputs()
	in.Artifact.Sources = []wire.ArtifactSource{
		{Kind: "hf", URI: "hf://zai-org/GLM-5.3-Flash@03eb5366286afd40d2221b1d9c63a6dd1ba4832e"},
	}

	ref, isPath, err := modelRef(in.Artifact)
	if err != nil {
		t.Fatalf("modelRef: %v", err)
	}
	if !isPath {
		t.Fatalf("a pinned source did not resolve to a path: %q", ref)
	}
	if !strings.HasPrefix(ref, "/") {
		t.Fatalf("model ref is not absolute: %q", ref)
	}
	// The same directory hfScript reconstructs to watch its own download. These two are the
	// writer and the reader of one path, and they must derive it identically.
	if want := HFSnapshotDir("zai-org/GLM-5.3-Flash", "03eb5366286afd40d2221b1d9c63a6dd1ba4832e"); ref != want {
		t.Fatalf("model ref = %q, want %q", ref, want)
	}
	if !strings.HasPrefix(ref, HFHome+"/hub/") {
		t.Fatalf("model ref is outside the shared cache: %q", ref)
	}
}

func TestTheObjectIsNamedAfterTheDeploymentAndFallsBackToItsID(t *testing.T) {
	cases := []struct{ name, want string }{
		{"chat-iad", "chat-iad"},
		{"Chat IAD (eu)", "chat-iad-eu"},
		{"", "dep-01j9x2"},
		{"!!!", "dep-01j9x2"},
		{strings.Repeat("a", 80), strings.Repeat("a", maxWorkloadName)},
	}
	for _, tc := range cases {
		in := testInputs()
		in.Deployment.Name = tc.name
		if got := WorkloadName(in.Deployment); got != tc.want {
			t.Fatalf("WorkloadName(%q) = %q, want %q", tc.name, got, tc.want)
		}
	}
}

// The pods Dynamo derives carry our labels only if they are on the template it derives them
// from — and without them every deployment this operator created came back as a sighting to
// adopt, because discovery reads pod labels to decide what it did not put there.
func TestTheOwnershipLabelsReachThePodTemplate(t *testing.T) {
	obj, err := DynamoGraphDeployment(testInputs(), testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	labels, found, err := unstructured.NestedStringMap(component(t, obj), "podTemplate", "metadata", "labels")
	if err != nil || !found {
		t.Fatalf("podTemplate.metadata.labels missing: found %v, err %v", found, err)
	}
	if labels["hangar.dev/deployment-id"] != "dep_01J9X2" {
		t.Fatalf("deployment-id label = %q", labels["hangar.dev/deployment-id"])
	}
	if labels["hangar.dev/managed-by"] != "hangar-operator" {
		t.Fatalf("managed-by label = %q", labels["hangar.dev/managed-by"])
	}
	if obj.GetLabels()["hangar.dev/deployment-id"] != "dep_01J9X2" {
		t.Fatalf("graph labels = %v", obj.GetLabels())
	}
}

// The regression this exists for: a name that fits the graph deployment and not the objects Dynamo
// derives from it. The child DynamoComponentDeployment's name is also a label value, so it is held
// to 63 bytes, and a graph name three characters too long left the workload
// `nvidia-nemotron-3-5-lightning-30b-a3b-bf16` with no pod ever created.
func TestTheDerivedComponentNameFitsALabelValue(t *testing.T) {
	in := testInputs()
	in.Deployment.Name = strings.Repeat("a", 200)
	child := WorkloadName(in.Deployment) + "-" + strings.ToLower(WorkerComponent) + "-" + specHashPlaceholder
	if len(child) > maxObjectNameBytes {
		t.Fatalf("the derived component name is %d bytes, over the %d a label value holds: %s",
			len(child), maxObjectNameBytes, child)
	}
}

// Every component this renderer emits has to fit `componentNameBudget`, which is the contract that
// replaced a bound derived from the components themselves.
//
// The direction matters and is the whole point. The old `maxWorkloadName` was `63 - max(suffixes)`,
// so adding a longer component shortened the bound and silently renamed deployments that had
// nothing to do with it — a rename being a delete, a recreate and a weight reload. Now the number
// is fixed and the components are held to it, so the same mistake is this test failing with the
// offending name in the message.
func TestComponentBudget(t *testing.T) {
	for _, name := range []string{WorkerComponent, PrefillComponent, FrontendComponent} {
		if got := len(strings.ToLower(name)); got > componentNameBudget {
			t.Fatalf("component %s is %d bytes, over the %d componentNameBudget allows; "+
				"a shorter name is the fix, not a smaller maxWorkloadName", name, got, componentNameBudget)
		}
	}
}

// TestServedModelName is what a client's `model` field resolves to. The plane sends the string
// already resolved, so what is asserted here is that the renderer derives nothing of its own and
// that the escape hatch still wins.
func TestServedModelName(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*Inputs)
		want   string
	}{
		{
			name:   "the plane's resolved name is what is rendered",
			mutate: func(in *Inputs) { in.Deployment.ServedModelName = "qwen/qwen3-8b" },
			want:   "qwen/qwen3-8b",
		},
		{
			name:   "the name is trimmed",
			mutate: func(in *Inputs) { in.Deployment.ServedModelName = "  acme/qwen-canary  " },
			want:   "acme/qwen-canary",
		},
		{
			// engine_args is laid over the derived flags, and an escape hatch that loses
			// to a default is not one.
			name: "engine args still win over everything",
			mutate: func(in *Inputs) {
				in.Deployment.ServedModelName = "acme/qwen-canary"
				in.Deployment.Serving.EngineArgs = map[string]string{"--served-model-name": "acme/qwen-hotfix"}
			},
			want: "acme/qwen-hotfix",
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
			args := renderedPod(t, obj).Containers[0].Args
			got := ""
			for i, arg := range args {
				if arg == "--served-model-name" && i+1 < len(args) {
					got = args[i+1]
				}
			}
			if got != tc.want {
				t.Fatalf("--served-model-name = %q, want %q", got, tc.want)
			}
		})
	}
}

// ------------------------------------------------------------------ one frontend per model

func frontendOptions() Options {
	opts := testOptions()
	opts.FrontendEnabled = true
	return opts
}

// componentNamed returns the rendered component with the given name, and fails when the graph
// does not hold exactly one of it.
func componentNamed(t *testing.T, obj *unstructured.Unstructured, name string) map[string]any {
	t.Helper()
	comps, found, err := unstructured.NestedSlice(obj.Object, "spec", "components")
	if err != nil || !found {
		t.Fatalf("spec.components missing: found %v, err %v", found, err)
	}
	var out map[string]any
	for _, entry := range comps {
		c, ok := entry.(map[string]any)
		if ok && c["name"] == name {
			if out != nil {
				t.Fatalf("two components named %q", name)
			}
			out = c
		}
	}
	if out == nil {
		t.Fatalf("no component named %q in %v", name, comps)
	}
	return out
}

func componentPod(t *testing.T, c map[string]any) corev1.PodSpec {
	t.Helper()
	raw, found, err := unstructured.NestedMap(c, "podTemplate", "spec")
	if err != nil || !found {
		t.Fatalf("podTemplate.spec missing: found %v, err %v", found, err)
	}
	var pod corev1.PodSpec
	if err := runtime.DefaultUnstructuredConverter.FromUnstructured(raw, &pod); err != nil {
		t.Fatalf("decode pod spec: %v", err)
	}
	return pod
}

// The migration control, and the only reason it is safe to ship this before any cluster has moved:
// with the option off the rendered object is what it always was, one component and no frontend.
func TestNoFrontendIsRenderedUntilTheClusterOptsIn(t *testing.T) {
	obj, err := DynamoGraphDeployment(testInputs(), testOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	comps, _, err := unstructured.NestedSlice(obj.Object, "spec", "components")
	if err != nil {
		t.Fatalf("components: %v", err)
	}
	if len(comps) != 1 {
		t.Fatalf("components = %v, want the worker alone", comps)
	}
	if first, _ := comps[0].(map[string]any); first["name"] != WorkerComponent {
		t.Fatalf("the one component is %v, want the worker", first["name"])
	}
}

// A frontend per model, in the graph the worker is already in. The component's name is what both
// label-based lookups match on and its type is what earns it its ports and its command line, so
// both are asserted: they are different fields saying different things.
func TestAFrontendIsRenderedBesideTheWorker(t *testing.T) {
	obj, err := DynamoGraphDeployment(testInputs(), frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	comps, _, err := unstructured.NestedSlice(obj.Object, "spec", "components")
	if err != nil {
		t.Fatalf("components: %v", err)
	}
	if len(comps) != 2 {
		t.Fatalf("components = %v, want the worker and its frontend", comps)
	}

	frontend := componentNamed(t, obj, FrontendComponent)
	if frontend["type"] != "frontend" {
		t.Fatalf("type = %v, want frontend", frontend["type"])
	}
	// Two, so that a rollout does not take the model's only frontend with it.
	if frontend["replicas"] != int64(2) {
		t.Fatalf("replicas = %v, want the default two", frontend["replicas"])
	}
	// Today's behaviour on every cluster: one Dynamo namespace, everything discovering
	// everything. Narrowing it is a setting and not yet an answer.
	if frontend["globalDynamoNamespace"] != true {
		t.Fatalf("globalDynamoNamespace = %v", frontend["globalDynamoNamespace"])
	}
	// The worker is untouched by any of this.
	worker := componentNamed(t, obj, WorkerComponent)
	if worker["globalDynamoNamespace"] != true || worker["replicas"] != int64(2) {
		t.Fatalf("the worker component changed: %v", worker)
	}
}

// The routing key. Dynamo copies a component's pod-template labels onto the Service it derives, so
// hangar.dev/deployment-id here is what lets the agent pick this frontend for a request the
// gateway has already resolved to this deployment. Without it the Service is anonymous and the
// deployment is reachable by nothing.
func TestTheFrontendPodTemplateCarriesTheOwnershipLabels(t *testing.T) {
	obj, err := DynamoGraphDeployment(testInputs(), frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	labels, found, err := unstructured.NestedStringMap(
		componentNamed(t, obj, FrontendComponent), "podTemplate", "metadata", "labels")
	if err != nil || !found {
		t.Fatalf("frontend podTemplate.metadata.labels missing: found %v, err %v", found, err)
	}
	if labels["hangar.dev/deployment-id"] != "dep_01J9X2" {
		t.Fatalf("deployment-id label = %q; the agent has no way to pick this frontend", labels["hangar.dev/deployment-id"])
	}
	if labels["hangar.dev/managed-by"] != "hangar-operator" {
		t.Fatalf("managed-by label = %q", labels["hangar.dev/managed-by"])
	}
}

// The frontend holds no weights, so a GPU node selector or a toleration for the GPU taint would
// put a router on an accelerator node and leave its cards doing nothing. Left alone it lands on
// ordinary nodes without being told to, because the GPU pool is tainted.
func TestTheFrontendIsKeptOffTheAcceleratorNodes(t *testing.T) {
	obj, err := DynamoGraphDeployment(testInputs(), frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	pod := componentPod(t, componentNamed(t, obj, FrontendComponent))
	if len(pod.NodeSelector) != 0 {
		t.Fatalf("node selector = %v, want none", pod.NodeSelector)
	}
	if len(pod.Tolerations) != 0 {
		t.Fatalf("tolerations = %v; a frontend that tolerates the GPU taint schedules onto a card it never uses", pod.Tolerations)
	}
	main := pod.Containers[0]
	if _, ok := main.Resources.Requests["nvidia.com/gpu"]; ok {
		t.Fatalf("the frontend asks for accelerators: %v", main.Resources.Requests)
	}
	// The worker still gets both, unchanged.
	worker := componentPod(t, componentNamed(t, obj, WorkerComponent))
	if worker.NodeSelector["hangar.io/pool"] != "gpu" || len(worker.Tolerations) != 1 {
		t.Fatalf("the worker's scheduling changed: selector=%v tolerations=%v", worker.NodeSelector, worker.Tolerations)
	}
}

// A frontend image is not always the engine image: the Kimi K3 fix is a patched frontend beside a
// stock worker, because both defects are in the frontend binary.
func TestTheFrontendImageIsResolvedMostSpecificFirst(t *testing.T) {
	const patched = "ghcr.io/hangar-dev/vllm-runtime:1.4.0-patched.1-hgr2"
	const clusterDefault = "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.4.1"

	cases := []struct {
		name     string
		declared *string
		cluster  string
		want     string
	}{
		{"the deployment wins", ptr(patched), clusterDefault, patched},
		{"the cluster default is next", nil, clusterDefault, clusterDefault},
		{"the engine image is the last resort", nil, "", prefetchImage},
		{"an empty declared field is not an override", ptr(""), clusterDefault, clusterDefault},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			in := testInputs()
			in.Deployment.Serving.FrontendImage = tc.declared
			opts := frontendOptions()
			opts.FrontendImage = tc.cluster
			obj, err := DynamoGraphDeployment(in, opts)
			if err != nil {
				t.Fatalf("render: %v", err)
			}
			pod := componentPod(t, componentNamed(t, obj, FrontendComponent))
			if pod.Containers[0].Image != tc.want {
				t.Fatalf("frontend image = %q, want %q", pod.Containers[0].Image, tc.want)
			}
			// The frontend's image never reaches the engine.
			worker := componentPod(t, componentNamed(t, obj, WorkerComponent))
			if worker.Containers[0].Image != prefetchImage {
				t.Fatalf("worker image = %q, want the engine's", worker.Containers[0].Image)
			}
		})
	}
}

// The seed is the one that has to match the worker's. KV-aware routing hashes block contents,
// Python salts hashes per process, and a frontend and worker that disagree miss every cache hit
// with nothing reporting it.
func TestTheFrontendCarriesTheSeedAndTheAnthropicSwitches(t *testing.T) {
	opts := frontendOptions()
	opts.AnthropicAPIEnabled = true
	opts.AnthropicStripPreamble = false
	obj, err := DynamoGraphDeployment(testInputs(), opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	main := componentPod(t, componentNamed(t, obj, FrontendComponent)).Containers[0]

	seed, ok := envValue(main, "PYTHONHASHSEED")
	if !ok || seed != "0" {
		t.Fatalf("PYTHONHASHSEED = %q, %v; the frontend and the worker must salt identically", seed, ok)
	}
	worker := componentPod(t, componentNamed(t, obj, WorkerComponent)).Containers[0]
	if workerSeed, _ := envValue(worker, "PYTHONHASHSEED"); workerSeed != seed {
		t.Fatalf("the two ends disagree on the seed: frontend %q, worker %q", seed, workerSeed)
	}

	if v, ok := envValue(main, "DYN_ENABLE_ANTHROPIC_API"); !ok || v != "true" {
		t.Fatalf("DYN_ENABLE_ANTHROPIC_API = %q, %v", v, ok)
	}
	if v, ok := envValue(main, "DYN_STRIP_ANTHROPIC_PREAMBLE"); !ok || v != "false" {
		t.Fatalf("DYN_STRIP_ANTHROPIC_PREAMBLE = %q, %v", v, ok)
	}
	// Set as environment and not arguments: the CRD owns the frontend's command line.
	if len(main.Command) != 0 || len(main.Args) != 0 {
		t.Fatalf("the frontend was given a command line: %v %v", main.Command, main.Args)
	}
}

// A private image with no credential is an ImagePullBackOff nothing reports upward. Engine
// credentials reach both the worker and the frontend that can fall back to that same image;
// frontend-only credentials stay off the worker.
func TestTheFrontendAndEngineTakeTheirPullSecrets(t *testing.T) {
	opts := frontendOptions()
	opts.FrontendPullSecrets = []string{"ghcr-frontend", "ghcr-shared"}
	opts.EnginePullSecrets = []string{"ghcr-engine", "ghcr-shared"}
	obj, err := DynamoGraphDeployment(testInputs(), opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	pod := componentPod(t, componentNamed(t, obj, FrontendComponent))
	if len(pod.ImagePullSecrets) != 3 ||
		pod.ImagePullSecrets[0].Name != "ghcr-frontend" ||
		pod.ImagePullSecrets[1].Name != "ghcr-shared" ||
		pod.ImagePullSecrets[2].Name != "ghcr-engine" {
		t.Fatalf("imagePullSecrets = %v", pod.ImagePullSecrets)
	}
	worker := componentPod(t, componentNamed(t, obj, WorkerComponent))
	if len(worker.ImagePullSecrets) != 2 ||
		worker.ImagePullSecrets[0].Name != "ghcr-engine" ||
		worker.ImagePullSecrets[1].Name != "ghcr-shared" {
		t.Fatalf("worker imagePullSecrets = %v", worker.ImagePullSecrets)
	}
}

// Both are settings so that a cluster can answer them without a code change — the replica count
// because a rollout must not be an outage, the namespace because whether a frontend can be
// narrowed to its own model is unresolved and needs an experiment.
func TestTheFrontendReplicaCountAndNamespaceAreClusterSettings(t *testing.T) {
	opts := frontendOptions()
	opts.FrontendReplicas = 3
	opts.FrontendGlobalNamespace = ptr(false)
	obj, err := DynamoGraphDeployment(testInputs(), opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	frontend := componentNamed(t, obj, FrontendComponent)
	if frontend["replicas"] != int64(3) {
		t.Fatalf("replicas = %v", frontend["replicas"])
	}
	if frontend["globalDynamoNamespace"] != false {
		t.Fatalf("globalDynamoNamespace = %v", frontend["globalDynamoNamespace"])
	}
	// Narrowing the frontend must not narrow the worker: the two flags are separate decisions
	// and flipping both at once is the migration nobody asked for.
	if worker := componentNamed(t, obj, WorkerComponent); worker["globalDynamoNamespace"] != true {
		t.Fatalf("the worker's namespace flag followed the frontend's: %v", worker)
	}
}

// A memory ceiling and no CPU one: the frontend copies every token of every response, so a CPU
// limit is latency on the data path.
func TestTheFrontendResourcesDefaultToWhatTheClusterFrontendRunsIn(t *testing.T) {
	obj, err := DynamoGraphDeployment(testInputs(), frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	res := componentPod(t, componentNamed(t, obj, FrontendComponent)).Containers[0].Resources
	if got := res.Requests.Cpu().String(); got != defaultFrontendCPU {
		t.Fatalf("cpu request = %s", got)
	}
	if got := res.Requests.Memory().String(); got != defaultFrontendMemory {
		t.Fatalf("memory request = %s", got)
	}
	if got := res.Limits.Memory().String(); got != defaultFrontendMemoryLimit {
		t.Fatalf("memory limit = %s", got)
	}
	if _, capped := res.Limits[corev1.ResourceCPU]; capped {
		t.Fatalf("a CPU limit was set on the data path: %v", res.Limits)
	}
}

// A quantity that does not parse, or a ceiling under the request, is a pod the API server takes
// and the scheduler never places. Saying so here names the setting.
func TestTheFrontendRefusesResourcesThatWouldNotSchedule(t *testing.T) {
	cases := []struct{ name, cpu, memory, limit, want string }{
		{"cpu is not a quantity", "half", "", "", "frontend cpu request"},
		{"memory is not a quantity", "", "lots", "", "frontend memory request"},
		{"the limit is not a quantity", "", "", "plenty", "frontend memory limit"},
		{"the limit is under the request", "", "4Gi", "1Gi", "below the request"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			opts := frontendOptions()
			opts.FrontendCPU, opts.FrontendMemory, opts.FrontendMemoryLimit = tc.cpu, tc.memory, tc.limit
			_, err := DynamoGraphDeployment(testInputs(), opts)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %v, want one naming %q", err, tc.want)
			}
		})
	}
}

// The overlay merges over the graph and lists replace, so an overlay written before there was a
// frontend to lose deletes it — leaving a graph that applies, workers that come up healthy, and a
// model no request can reach. Nothing else catches that, so this is a refusal.
func TestAnOverlayThatDeletesTheFrontendIsRefused(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.Overlay.Dynamo = map[string]any{
		"components": []any{
			map[string]any{"name": "VllmPrefillWorker", "replicas": int64(1)},
			map[string]any{"name": WorkerComponent, "replicas": int64(3)},
		},
	}
	_, err := DynamoGraphDeployment(in, frontendOptions())
	if err == nil {
		t.Fatal("an overlay that deletes the frontend rendered without complaint")
	}
	if !strings.Contains(err.Error(), in.Deployment.DeploymentID) {
		t.Errorf("the refusal does not name the deployment: %v", err)
	}
	if !strings.Contains(err.Error(), FrontendComponent) {
		t.Errorf("the refusal does not name what was lost: %v", err)
	}
	if !strings.Contains(err.Error(), "spec.components") {
		t.Errorf("the refusal does not say what to do about it: %v", err)
	}
}

// The escape hatch stays open: an overlay may still hand-write a topology this renderer cannot
// derive, so long as the frontend is still in it and still carries the label a request finds it by.
func TestAnOverlayThatRestatesTheFrontendIsAccepted(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.Overlay.Dynamo = map[string]any{
		"components": []any{
			map[string]any{"name": "VllmPrefillWorker", "replicas": int64(1)},
			map[string]any{"name": WorkerComponent, "replicas": int64(3)},
			map[string]any{
				"name": FrontendComponent, "type": "frontend", "replicas": int64(1),
				"podTemplate": map[string]any{"metadata": map[string]any{"labels": map[string]any{
					deploymentIDLabel: in.Deployment.DeploymentID,
				}}},
			},
		},
	}
	if _, err := DynamoGraphDeployment(in, frontendOptions()); err != nil {
		t.Fatalf("render: %v", err)
	}
}

// A frontend that is in the graph and anonymous fails exactly the way a missing one does: Dynamo
// derives a Service from it carrying no `hangar.dev/deployment-id`, so nothing the gateway has
// already resolved to this deployment can find it. Checking only that the component is present
// would let the guard pass the defect it exists for.
func TestAnOverlayThatRestatesTheFrontendWithoutItsRoutingLabelIsRefused(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.Overlay.Dynamo = map[string]any{
		"components": []any{
			map[string]any{"name": WorkerComponent, "replicas": int64(3)},
			map[string]any{"name": FrontendComponent, "type": "frontend", "replicas": int64(1)},
		},
	}
	_, err := DynamoGraphDeployment(in, frontendOptions())
	if err == nil {
		t.Fatal("an anonymous frontend rendered without complaint; nothing could route to it")
	}
	if !strings.Contains(err.Error(), deploymentIDLabel) {
		t.Errorf("the refusal does not name the label that is missing: %v", err)
	}
	if !strings.Contains(err.Error(), in.Deployment.DeploymentID) {
		t.Errorf("the refusal does not say what the label has to say: %v", err)
	}
}

// A cluster that has not opted in has no frontend in the graph to lose, so an overlay that
// replaces the components is what it always was: the intended way to hand-write a topology.
func TestTheOverlayRefusalDoesNotApplyBeforeTheClusterOptsIn(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.Overlay.Dynamo = map[string]any{
		"components": []any{map[string]any{"name": WorkerComponent, "replicas": int64(3)}},
	}
	if _, err := DynamoGraphDeployment(in, testOptions()); err != nil {
		t.Fatalf("render: %v", err)
	}
}

// The name bound has to hold for every component the graph actually holds, not for the worker
// alone. The worker's derived name is the longest today, so nothing moves — but a bound derived
// from one component is right by coincidence, and the overflow it stops is silent: the API server
// refuses a child object this operator never reads, and the deployment sits at zero replicas
// ready with no pod and no explanation.
func TestTheNameBoundHoldsForEveryComponentRendered(t *testing.T) {
	in := testInputs()
	in.Deployment.Name = strings.Repeat("a", 200)
	obj, err := DynamoGraphDeployment(in, frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	comps, _, err := unstructured.NestedSlice(obj.Object, "spec", "components")
	if err != nil {
		t.Fatalf("components: %v", err)
	}
	if len(comps) < 2 {
		t.Fatalf("components = %v, want more than the worker to bound against", comps)
	}
	for _, entry := range comps {
		c, _ := entry.(map[string]any)
		name, _ := c["name"].(string)
		derived := obj.GetName() + "-" + strings.ToLower(name) + "-" + specHashPlaceholder
		if len(derived) > maxObjectNameBytes {
			t.Fatalf("the name derived for %s is %d bytes, over the %d a label value holds: %s",
				name, len(derived), maxObjectNameBytes, derived)
		}
	}
}

// Dynamo names each DynamoComponentDeployment `<graph>-<component>-<hash>`, where the hash covers
// the whole component pod template — labels included. So the set below is not bookkeeping: it is
// an input to a name, and changing it renames every component in the fleet, which Dynamo answers
// by abandoning the running one and starting a replacement that reloads the model's weights.
//
// That is exactly what happened on 2026-09-02. Deleting Recipe removed `hangar.dev/recipe` from
// `ownershipLabels`, no code anywhere read it, and every decode worker on every cluster rolled.
// On site-c, which was full, the roll deadlocked: the replacement could not schedule until the
// original released its eight cards, and the original was not going anywhere.
//
// The reconciler no longer applies a graph deployment whose inputs are unchanged, so a set edited
// here reaches only workloads whose declaration moves afterwards. This test is the second half of
// that: it makes an edit to the set deliberate rather than a side effect of tidying up somewhere
// else. Adding or removing a label is allowed — update the list and know that you are paying a
// reload for anything that redeploys.
func TestThePodTemplateLabelSetIsExactlyThese(t *testing.T) {
	want := []string{
		"hangar.dev/deployment-id",
		"hangar.dev/managed-by",
		"hangar.dev/project",
	}

	obj, err := DynamoGraphDeployment(testInputs(), frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	for _, name := range []string{WorkerComponent, FrontendComponent} {
		labels, found, err := unstructured.NestedStringMap(
			componentNamed(t, obj, name), "podTemplate", "metadata", "labels")
		if err != nil || !found {
			t.Fatalf("%s podTemplate.metadata.labels missing: found %v, err %v", name, found, err)
		}
		got := make([]string, 0, len(labels))
		for key := range labels {
			got = append(got, key)
		}
		sort.Strings(got)
		if !slices.Equal(got, want) {
			t.Fatalf("%s pod-template labels = %v, want %v — every workload that redeploys "+
				"reloads its weights for this change", name, got, want)
		}
	}
}

// The regression that took Kimi K3 off the fleet, pinned at the layer that caused it.
//
// `modelRef` points --model at the snapshot directory, which is the only form every engine reads
// the same way. That string is also the identity of the model card the worker publishes, and the
// frontend resolves the card to build its tokenizer and prompt formatter — so a frontend with no
// access to that path resolves nothing, registers nothing, and answers 404 for the model while
// every count in the cluster reads healthy.
func TestTheFrontendCanReadTheSnapshotTheWorkerIsPointedAt(t *testing.T) {
	opts := frontendOptions()
	opts.ModelCachePVC = "model-cache"

	obj, err := DynamoGraphDeployment(testInputs(), opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	front := componentNamed(t, obj, FrontendComponent)
	spec := componentPod(t, front)

	var mount *corev1.VolumeMount
	for i := range spec.Containers[0].VolumeMounts {
		if spec.Containers[0].VolumeMounts[i].MountPath == "/models" {
			mount = &spec.Containers[0].VolumeMounts[i]
		}
	}
	if mount == nil {
		t.Fatalf("the frontend cannot reach the path --model names: %+v", spec.Containers[0].VolumeMounts)
	}
	// Read-only, because the volume belongs to the prefetch job and a frontend comes for 30 MB
	// of metadata it will only ever read.
	if !mount.ReadOnly {
		t.Error("the frontend mounts the cache writable")
	}
	if len(spec.Volumes) == 0 || spec.Volumes[0].PersistentVolumeClaim == nil ||
		spec.Volumes[0].PersistentVolumeClaim.ClaimName != "model-cache" {
		t.Errorf("the frontend volume is not the cluster's cache claim: %+v", spec.Volumes)
	}
}

// A cluster with no cache claim configured gets a frontend without the volume, rather than one
// naming an empty claim — which the API server rejects, taking the whole graph with it.
func TestAFrontendWithNoCacheClaimRendersNoVolume(t *testing.T) {
	opts := frontendOptions()
	opts.ModelCachePVC = ""

	obj, err := DynamoGraphDeployment(testInputs(), opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	spec := componentPod(t, componentNamed(t, obj, FrontendComponent))
	if len(spec.Volumes) != 0 || len(spec.Containers[0].VolumeMounts) != 0 {
		t.Errorf("a volume was invented with no claim to name: %+v", spec.Volumes)
	}
}

// The frontend still takes none of the engine's other machinery. The mount is the one thing the
// two share, and the reason the overlay is refused has not changed: an overlay written about the
// engine must not reach a process that holds no weights and no cards.
func TestTheFrontendTakesTheCacheAndNothingElseFromTheEngine(t *testing.T) {
	opts := frontendOptions()

	obj, err := DynamoGraphDeployment(testInputs(), opts)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	spec := componentPod(t, componentNamed(t, obj, FrontendComponent))
	if len(spec.Containers[0].VolumeMounts) != 1 {
		t.Errorf("the frontend took more than the cache: %+v", spec.Containers[0].VolumeMounts)
	}
	if spec.Containers[0].Resources.Limits != nil {
		if _, ok := spec.Containers[0].Resources.Limits["nvidia.com/gpu"]; ok {
			t.Error("the frontend asks for an accelerator")
		}
	}
	if len(spec.Tolerations) != 0 || spec.NodeSelector != nil {
		t.Error("the frontend is being steered onto the accelerator pool")
	}
}

// disaggInputs is testInputs with a 1:2 disaggregated topology declared on it, per-role arguments
// and all.
func disaggInputs() Inputs {
	in := testInputs()
	in.Deployment.Serving.Topology = "disaggregated"
	in.Deployment.Serving.Roles = []wire.RoleSpec{
		{Role: "prefill", MaxBatchTokens: ptr(32768), EngineArgs: map[string]string{"max-num-seqs": "4"}},
		{Role: "decode", MaxBatchTokens: ptr(2048), EngineArgs: map[string]string{"max-num-seqs": "256"}},
	}
	in.Deployment.RoleReplicas = map[string]int{"prefill": 2, "decode": 4}
	return in
}

// The bar every other test in this file is held to once, stated as its own test: an aggregated
// deployment must render byte-for-byte what it rendered before disaggregation existed.
//
// Not a nicety. The rendered pod template is what Dynamo hashes into the object name, so a single
// added label or reordered flag renames every worker in the fleet — and a rename is a delete, a
// recreate and a weight reload, twenty minutes each for a large model, on clusters where nothing
// was declared to have changed. `FrontendEnabled` sets itself the same bar and for the same reason.
func TestAnAggregatedRenderIsUnchangedByDisaggregationExisting(t *testing.T) {
	plain := testInputs()

	// The same deployment with the fields present but empty, which is what a plane that has been
	// upgraded sends for every workload nobody has disaggregated.
	empty := testInputs()
	empty.Deployment.Serving.Topology = "aggregated"
	empty.Deployment.Serving.Roles = nil
	empty.Deployment.RoleReplicas = nil

	want, err := DynamoGraphDeployment(plain, frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	got, err := DynamoGraphDeployment(empty, frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if !reflect.DeepEqual(want.Object, got.Object) {
		t.Fatalf("an aggregated render moved when the topology fields were set to their empty meaning:\nwant %#v\ngot  %#v",
			want.Object, got.Object)
	}
	// And the worker it renders is still the one worker, under the name it always had.
	comps, _, err := unstructured.NestedSlice(got.Object, "spec", "components")
	if err != nil {
		t.Fatalf("components: %v", err)
	}
	var comp map[string]any
	for _, entry := range comps {
		c, _ := entry.(map[string]any)
		if c["name"] == WorkerComponent {
			comp = c
		}
	}
	if comp == nil {
		t.Fatalf("no %s component in %v", WorkerComponent, comps)
	}
	if comp["name"] != WorkerComponent {
		t.Fatalf("component name = %v, want %v", comp["name"], WorkerComponent)
	}
	if comp["replicas"] != int64(2) {
		t.Fatalf("replicas = %v, want the deployment's own 2", comp["replicas"])
	}
	if _, found := comp["podTemplate"].(map[string]any)["metadata"].(map[string]any)["labels"].(map[string]any)[RoleLabel]; found {
		t.Fatalf("an aggregated worker carries a role label, which renames every pod in the fleet")
	}
}

// The two-component render: one graph, both roles, each with the count the plane sent.
func TestADisaggregatedDeploymentRendersBothRoles(t *testing.T) {
	obj, err := DynamoGraphDeployment(disaggInputs(), frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	comps, _, err := unstructured.NestedSlice(obj.Object, "spec", "components")
	if err != nil {
		t.Fatalf("components: %v", err)
	}
	if len(comps) != 3 {
		t.Fatalf("components = %d, want prefill, decode and the frontend", len(comps))
	}
	byName := map[string]map[string]any{}
	for _, entry := range comps {
		c, _ := entry.(map[string]any)
		byName[c["name"].(string)] = c
	}
	prefill, ok := byName[PrefillComponent]
	if !ok {
		t.Fatalf("no %s component in %v", PrefillComponent, byName)
	}
	decode, ok := byName[WorkerComponent]
	if !ok {
		t.Fatalf("no %s component in %v", WorkerComponent, byName)
	}
	if prefill["type"] != "prefill" || decode["type"] != "decode" {
		t.Fatalf("types = %v/%v, want prefill/decode", prefill["type"], decode["type"])
	}
	// Absolute counts, rendered exactly as sent. The plane multiplied groups by weights; this
	// operator must not multiply anything.
	if prefill["replicas"] != int64(2) {
		t.Fatalf("prefill replicas = %v, want the 2 the wire carried", prefill["replicas"])
	}
	if decode["replicas"] != int64(4) {
		t.Fatalf("decode replicas = %v, want the 4 the wire carried", decode["replicas"])
	}
}

// The flags that come from the topology rather than from anything a person typed.
func TestEachRoleGetsItsOwnDerivedFlagsAndArguments(t *testing.T) {
	for _, tc := range []struct {
		role           string
		wantMode       string
		wantBatch      string
		wantMaxNumSeqs string
	}{
		{"prefill", "prefill", "32768", "4"},
		{"decode", "decode", "2048", "256"},
	} {
		t.Run(tc.role, func(t *testing.T) {
			in := disaggInputs()
			roles, err := WorkerRoles(in.Deployment)
			if err != nil {
				t.Fatalf("workerRoles: %v", err)
			}
			var role workerRole
			for _, r := range roles {
				if r.Role == tc.role {
					role = r
				}
			}
			args, err := engineArgs(in, Options{}, role)
			if err != nil {
				t.Fatalf("engineArgs: %v", err)
			}
			flags := map[string]string{}
			for i := 0; i < len(args); i++ {
				if !strings.HasPrefix(args[i], "--") {
					continue
				}
				key := strings.TrimPrefix(args[i], "--")
				if i+1 < len(args) && !strings.HasPrefix(args[i+1], "--") {
					flags[key] = args[i+1]
				} else {
					flags[key] = ""
				}
			}
			if flags["disaggregation-mode"] != tc.wantMode {
				t.Fatalf("disaggregation-mode = %q, want %q", flags["disaggregation-mode"], tc.wantMode)
			}
			if flags["kv-transfer-config"] != kvTransferConfig {
				t.Fatalf("kv-transfer-config = %q, want the topology's own %q", flags["kv-transfer-config"], kvTransferConfig)
			}
			if flags["max-num-batched-tokens"] != tc.wantBatch {
				t.Fatalf("max-num-batched-tokens = %q, want the role's %q", flags["max-num-batched-tokens"], tc.wantBatch)
			}
			if flags["max-num-seqs"] != tc.wantMaxNumSeqs {
				t.Fatalf("max-num-seqs = %q, want the role's %q", flags["max-num-seqs"], tc.wantMaxNumSeqs)
			}
		})
	}
}

// An aggregated worker gets neither flag, which is the other half of the byte-identity bar.
func TestAnAggregatedWorkerGetsNoTopologyFlags(t *testing.T) {
	args, err := engineArgs(testInputs(), Options{}, workerRole{})
	if err != nil {
		t.Fatalf("engineArgs: %v", err)
	}
	for _, arg := range args {
		if arg == "--disaggregation-mode" || arg == "--kv-transfer-config" {
			t.Fatalf("an aggregated worker was given %s, which renames and reloads it", arg)
		}
	}
}

// A role with configuration and no count is a frame that lost half of itself. Rendering it at a
// default would serve a ratio nobody declared, so it is an error.
func TestARoleWithNoReplicaCountIsRefused(t *testing.T) {
	in := disaggInputs()
	delete(in.Deployment.RoleReplicas, "prefill")
	if _, err := DynamoGraphDeployment(in, frontendOptions()); err == nil {
		t.Fatal("rendered a role with no replica count on the wire, want an error")
	}
}

// Topology and roles have to agree before anything renders differently, so that neither half of a
// half-upgraded plane produces a graph nobody declared.
func TestTopologyAndRolesMustAgreeBeforeAnythingSplits(t *testing.T) {
	rolesOnly := testInputs()
	rolesOnly.Deployment.Serving.Roles = disaggInputs().Deployment.Serving.Roles
	if Disaggregated(rolesOnly.Deployment) {
		t.Fatal("roles without a topology read as disaggregated")
	}
	topologyOnly := testInputs()
	topologyOnly.Deployment.Serving.Topology = "disaggregated"
	if Disaggregated(topologyOnly.Deployment) {
		t.Fatal("a topology without roles reads as disaggregated")
	}
}

// The role label is what ingestion reads back to say which role a replica is.
func TestEachRolesPodsCarryTheirRole(t *testing.T) {
	obj, err := DynamoGraphDeployment(disaggInputs(), frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	comps, _, _ := unstructured.NestedSlice(obj.Object, "spec", "components")
	seen := map[string]string{}
	for _, entry := range comps {
		c, _ := entry.(map[string]any)
		name, _ := c["name"].(string)
		tmpl, ok := c["podTemplate"].(map[string]any)
		if !ok {
			continue
		}
		labels, _ := tmpl["metadata"].(map[string]any)["labels"].(map[string]any)
		if v, found := labels[RoleLabel]; found {
			seen[name], _ = v.(string)
		}
	}
	if seen[PrefillComponent] != "prefill" || seen[WorkerComponent] != "decode" {
		t.Fatalf("role labels = %v, want prefill and decode on their own components", seen)
	}
	if len(seen) != 2 {
		t.Fatalf("role labels = %v, want them on the two workers and nowhere else", seen)
	}
}

// The warm path, which is three flags and easy to get silently wrong.
//
// Measured on site-a on 2026-09-08: a 30k-token conversation growing 1.3k a turn held a 96.4% prefix
// cache hit rate against one prefill worker. That reuse is what these flags protect, and each of
// them fails silently on its own — as a cold cache and a latency nobody can attribute, never as an
// error.
func TestTheWarmPathIsRenderedForDisaggregatedDeployments(t *testing.T) {
	in := disaggInputs()
	in.Deployment.Serving.PrefixCacheEnabled = false

	roles, err := WorkerRoles(in.Deployment)
	if err != nil {
		t.Fatalf("workerRoles: %v", err)
	}
	for _, role := range roles {
		args, err := engineArgs(in, Options{}, role)
		if err != nil {
			t.Fatalf("engineArgs: %v", err)
		}
		joined := strings.Join(args, " ")
		switch role.Role {
		case "prefill":
			// Prefill is the only role whose prefix cache can be hit, so the workload's "off"
			// must not reach it: decode never prefills, and copying the flag to both roles is
			// what forecloses turn-to-turn reuse.
			if strings.Contains(joined, "--no-enable-prefix-caching") {
				t.Fatal("prefill caching was disabled, which forecloses every turn-to-turn cache hit")
			}
			if !strings.Contains(joined, "--kv-events-config") {
				t.Fatal("prefill publishes no KV events, so the router indexes an empty tree " +
					"and silently degrades to load balancing")
			}
		case "decode":
			if !strings.Contains(joined, "--no-enable-prefix-caching") {
				t.Fatal("decode kept its prefix cache, which buys nothing and costs KV blocks")
			}
			if strings.Contains(joined, "--kv-events-config") {
				t.Fatal("decode publishes KV events it has no prefix cache to advertise")
			}
		}
	}
}

// An aggregated worker keeps the workload's own answer, unconditionally. This is the other half of
// the byte-identity bar: the exemption above is for a disaggregated prefill role and nothing else.
func TestPrefixCachingIsUnchangedForAnAggregatedWorker(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.PrefixCacheEnabled = false
	args, err := engineArgs(in, Options{}, workerRole{})
	if err != nil {
		t.Fatalf("engineArgs: %v", err)
	}
	if !slices.Contains(args, "--no-enable-prefix-caching") {
		t.Fatal("an aggregated worker stopped honouring its own prefix cache setting")
	}
	if strings.Contains(strings.Join(args, " "), "--kv-events-config") {
		t.Fatal("an aggregated worker was given a KV events config, which renames and reloads it")
	}
}

// The frontend routes by KV whatever the topology, because it is the worker count and not the
// topology that breaks round-robin. Gated on disaggregation, this left every multi-replica
// aggregated model in the fleet routing cache-blind: measured on four B300s serving Kimi-K3, one
// replica at 86% KV usage and a 35.7s TTFT beside an idle one, and `router_kv_hit_rate` with 1701
// routed requests and zero observations.
func routerMode(t *testing.T, in Inputs) string {
	t.Helper()
	spec, err := frontendPodSpec(in, frontendOptions())
	if err != nil {
		t.Fatalf("frontendPodSpec: %v", err)
	}
	for _, env := range spec.Containers[0].Env {
		if env.Name == "DYN_ROUTER_MODE" {
			return env.Value
		}
	}
	return ""
}

func TestTheFrontendRoutesByKVWhateverTheTopology(t *testing.T) {
	for name, in := range map[string]Inputs{
		"aggregated":    testInputs(),
		"disaggregated": disaggInputs(),
	} {
		if got := routerMode(t, in); got != "kv" {
			t.Fatalf("%s frontend DYN_ROUTER_MODE = %q, want kv — it routes round-robin, "+
				"so every turn of a conversation lands on a worker that never saw its prefix", name, got)
		}
	}
}

// A one-replica deployment is rendered the same way, deliberately. The KV router with a single
// registered worker picks the only candidate — the choice round-robin would have made — so there
// is nothing to gain from a `Replicas > 1` gate and a moving autoscaler count to lose to it.
func TestTheFrontendRoutesByKVEvenAtOneReplica(t *testing.T) {
	in := testInputs()
	in.Deployment.MinReplicas = 1
	if got := routerMode(t, in); got != "kv" {
		t.Fatalf("single-replica frontend DYN_ROUTER_MODE = %q, want kv — the mode must not "+
			"depend on a replica count that moves under an autoscaler", got)
	}
}

// The other half of the pair, and the half that costs a rename. An aggregated worker prefills, so
// it holds exactly the prefix cache the router wants to find; without this flag upstream's
// `create_kv_events_config` returns None and the frontend's KV mode indexes an empty tree.
func TestAnAggregatedWorkerPublishesTheEventsTheRouterIndexes(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.PrefixCacheEnabled = true
	args, err := engineArgs(in, Options{}, workerRole{})
	if err != nil {
		t.Fatalf("engineArgs: %v", err)
	}
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "--kv-events-config") {
		t.Fatal("an aggregated worker publishes no KV events, so the frontend's KV router " +
			"indexes an empty tree and silently degrades to load balancing")
	}
	if !strings.Contains(joined, kvEventsConfig) {
		t.Fatalf("aggregated kv-events-config was not the config the frontend expects: %s", joined)
	}
}

// engine_args is the escape hatch and this flag is defaulted, not derived: the plane refuses
// `disaggregation-mode` and `kv-transfer-config` from engine_args and refuses this one from
// nobody, so a workload naming its own endpoint must not be overruled by the operator's default.
func TestAWorkloadMayNameItsOwnKVEventsConfig(t *testing.T) {
	const own = `{"publisher":"zmq","topic":"mine","endpoint":"tcp://*:29999","enable_kv_cache_events":true}`
	in := testInputs()
	in.Deployment.Serving.PrefixCacheEnabled = true
	in.Deployment.Serving.EngineArgs = map[string]string{"kv-events-config": own}
	args, err := engineArgs(in, Options{}, workerRole{})
	if err != nil {
		t.Fatalf("engineArgs: %v", err)
	}
	if !slices.Contains(args, own) {
		t.Fatalf("the operator's default overruled the workload's own kv-events-config: %v", args)
	}
	if slices.Contains(args, kvEventsConfig) {
		t.Fatal("both the workload's kv-events-config and the operator's default were rendered")
	}
}

func specAnnotations(t *testing.T, obj *unstructured.Unstructured) map[string]string {
	t.Helper()
	got, found, err := unstructured.NestedStringMap(obj.Object, "spec", "annotations")
	if err != nil || !found {
		t.Fatalf("spec.annotations missing: found %v, err %v", found, err)
	}
	return got
}

func TestEveryRenderCarriesTheRolloutStrategyThatDoesNotDeadlock(t *testing.T) {
	for name, in := range map[string]Inputs{
		"aggregated":    testInputs(),
		"disaggregated": disaggInputs(),
	} {
		obj, err := DynamoGraphDeployment(in, frontendOptions())
		if err != nil {
			t.Fatalf("%s render: %v", name, err)
		}
		got := specAnnotations(t, obj)
		want := map[string]string{
			MaxSurgeAnnotation:       "0",
			MaxUnavailableAnnotation: "1",
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("%s: spec.annotations = %v, want %v", name, got, want)
		}
	}
}

func TestAnOverlayStillOwnsTheRolloutStrategy(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.Overlay.Dynamo = map[string]any{
		"annotations": map[string]any{"nvidia.com/deployment-strategy": "Recreate"},
	}
	obj, err := DynamoGraphDeployment(in, frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	got := specAnnotations(t, obj)
	if got["nvidia.com/deployment-strategy"] != "Recreate" {
		t.Fatalf("the overlay did not reach spec.annotations: %v", got)
	}
	if got[MaxSurgeAnnotation] != "0" {
		t.Fatalf("an overlay that adds one annotation dropped the rendered ones: %v", got)
	}
}

// A cluster that names no local directory renders exactly what it rendered before this existed.
// The mirror is an optimisation, and an optimisation that changes the pod on a cluster which did
// not ask for it is a fleet-wide reload nobody declared.
func TestLocalModelCacheOffByDefault(t *testing.T) {
	in := testInputs()
	in.Artifact.Sources = []wire.ArtifactSource{
		{Kind: "hf", URI: "hf://openai/gpt-oss-20b@6cee5e81ee83917806bbde320786a8fb61efebee"},
	}
	spec, err := podSpec(in, Options{ModelCachePVC: "model-cache"}, workerRole{})
	if err != nil {
		t.Fatal(err)
	}
	if len(spec.InitContainers) != 0 {
		t.Errorf("init containers = %d, want 0 with no local dir named", len(spec.InitContainers))
	}
	for _, v := range spec.Volumes {
		if v.Name == "local-model-cache" {
			t.Error("rendered a host path volume on a cluster that named no local directory")
		}
	}
	args, err := engineArgs(in, Options{}, workerRole{})
	if err != nil {
		t.Fatal(err)
	}
	if model := argValue(args, "--model"); !strings.HasPrefix(model, "/models/") {
		t.Errorf("--model = %q, want it under the shared cache", model)
	}
}

// With a directory named, the engine reads the mirror and the init container fills it. The two
// have to agree on the path: a worker pointed at a directory nothing populates does not fail, it
// hangs on a download it cannot see, so the assertion is that the argument and the copy's
// destination are the same string.
func TestLocalModelCacheMirrorsSharedPath(t *testing.T) {
	opts := Options{ModelCachePVC: "model-cache", LocalModelCacheDir: "/var/lib/hangar/models"}
	in := testInputs()
	in.Artifact.Sources = []wire.ArtifactSource{
		{Kind: "hf", URI: "hf://openai/gpt-oss-20b@6cee5e81ee83917806bbde320786a8fb61efebee"},
	}
	spec, err := podSpec(in, opts, workerRole{})
	if err != nil {
		t.Fatal(err)
	}
	if len(spec.InitContainers) != 1 {
		t.Fatalf("init containers = %d, want 1", len(spec.InitContainers))
	}
	init := spec.InitContainers[0]
	if len(init.Command) != 6 {
		t.Fatalf("command = %v, want sh -c script $0 src dst", init.Command)
	}
	shared, dst := init.Command[4], init.Command[5]
	if !strings.HasPrefix(shared, "/models/") {
		t.Errorf("source = %q, want the shared cache", shared)
	}
	if want, _ := localModelPath(shared); dst != want {
		t.Errorf("destination = %q, want %q", dst, want)
	}
	if len(init.Resources.Limits) != 0 {
		t.Errorf("init container asks for %v; a file copy holds no accelerator", init.Resources.Limits)
	}

	args, err := engineArgs(in, opts, workerRole{})
	if err != nil {
		t.Fatal(err)
	}
	if model := argValue(args, "--model"); model != dst {
		t.Errorf("--model = %q, but the copy writes %q; the engine would read an empty path", model, dst)
	}

	var host *corev1.Volume
	for i := range spec.Volumes {
		if spec.Volumes[i].Name == "local-model-cache" {
			host = &spec.Volumes[i]
		}
	}
	if host == nil || host.HostPath == nil {
		t.Fatal("no host path volume for the local mirror")
	}
	if host.HostPath.Path != opts.LocalModelCacheDir {
		t.Errorf("host path = %q, want %q", host.HostPath.Path, opts.LocalModelCacheDir)
	}
}

// argValue reads the value of a flag from a rendered command line.
func argValue(args []string, flag string) string {
	for i, arg := range args {
		if arg == flag && i+1 < len(args) {
			return args[i+1]
		}
	}
	return ""
}

// Liveness must be slower to act than readiness. The two probes answer the same endpoint family on
// a worker whose health reflects whether it is accepting rather than whether it is alive, so the
// only thing separating a saturated worker from a dead one is how long each probe waits. Readiness
// takes it out of rotation; liveness destroys a process holding a 1.4 TB model.
func TestWorkerProbesDoNotKillASaturatedWorker(t *testing.T) {
	spec, err := podSpec(testInputs(), Options{ModelCachePVC: "model-cache"}, workerRole{})
	if err != nil {
		t.Fatal(err)
	}
	main := spec.Containers[0]
	if main.LivenessProbe == nil || main.ReadinessProbe == nil {
		t.Fatal("worker renders no probes; Dynamo's failureThreshold-1 default would apply")
	}

	live := main.LivenessProbe
	if live.FailureThreshold < 3 {
		t.Errorf("liveness failureThreshold = %d; one missed check must not kill a worker", live.FailureThreshold)
	}
	liveBudget := live.FailureThreshold * live.PeriodSeconds
	readyBudget := main.ReadinessProbe.FailureThreshold * main.ReadinessProbe.PeriodSeconds
	if liveBudget <= readyBudget {
		t.Errorf("liveness tolerates %ds and readiness %ds; liveness must be the slower of the two, "+
			"or a busy worker is killed before it is merely de-routed", liveBudget, readyBudget)
	}
	if liveBudget < 120 {
		t.Errorf("liveness tolerates only %ds; a loaded worker stalls longer than that", liveBudget)
	}
}

// The offload tier is off unless a workload asks for it, and asking for it must not change decode.
//
// Both halves matter. The first is what makes the tier measurable — the same fleet renders with and
// without it from a workload edit — and the second is the asymmetry the tier is built on: prefill
// is the only role whose cache can be hit, so decode offloading would spend host bandwidth writing
// blocks nothing reads.
func TestHostOffloadTierIsOptInAndPrefillOnly(t *testing.T) {
	render := func(cfg map[string]string, role string) []string {
		in := testInputs()
		in.Deployment.Serving.PrefixCacheConfig = cfg
		args, err := engineArgs(in, Options{ModelCachePVC: "model-cache"}, workerRole{Role: role})
		if err != nil {
			t.Fatal(err)
		}
		return args
	}

	for _, role := range []string{"prefill", "decode"} {
		if got := argValue(render(nil, role), "--kv-transfer-config"); got != kvTransferConfig {
			t.Errorf("%s with no prefix_cache_config renders %q; the tier must be opt-in", role, got)
		}
	}

	on := map[string]string{"offload": "host", "offload_gib": "256"}
	if got := argValue(render(on, "decode"), "--kv-transfer-config"); got != kvTransferConfig {
		t.Errorf("decode renders %q with the tier on; decode never prefills and must not offload", got)
	}

	prefill := argValue(render(on, "prefill"), "--kv-transfer-config")
	if !strings.Contains(prefill, `"kv_connector":"MultiConnector"`) ||
		!strings.Contains(prefill, `"kv_connector":"OffloadingConnector"`) ||
		!strings.Contains(prefill, `"kv_connector":"NixlConnector"`) {
		t.Fatalf("prefill renders %q; it must wrap both the offload and the transport connector", prefill)
	}
	// GiB in, bytes out — the conversion is the whole reason the field is not passed through.
	if want := `"cpu_bytes_to_use":274877906944`; !strings.Contains(prefill, want) {
		t.Errorf("prefill renders %q; 256 GiB must reach the engine as %s", prefill, want)
	}
	// The JSON reaches an engine that parses it, so it has to actually be JSON.
	var parsed map[string]any
	if err := json.Unmarshal([]byte(prefill), &parsed); err != nil {
		t.Fatalf("prefill kv-transfer-config is not valid JSON: %v", err)
	}
}

// An unbounded host pool on a node running one worker per eight GPUs means the OOM killer chooses
// between the pool and the thing being measured. The ceiling has to leave room for everything the
// worker needed before the pool existed, which is what the declared request states.
func TestOffloadPoolBoundsWorkerMemory(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.PrefixCacheConfig = map[string]string{"offload": "host", "offload_gib": "256"}
	mib := int64(128 * 1024)
	in.Deployment.Serving.Resources.MemoryMiB = &mib

	spec, err := podSpec(in, Options{ModelCachePVC: "model-cache"}, workerRole{Role: "prefill"})
	if err != nil {
		t.Fatal(err)
	}
	limit, ok := spec.Containers[0].Resources.Limits[corev1.ResourceMemory]
	if !ok {
		t.Fatal("no memory limit with the offload tier on; the pool would grow into the node")
	}
	if want := int64(384) * 1024 * 1024 * 1024; limit.Value() != want {
		t.Errorf("memory limit = %d, want %d (128 GiB requested + a 256 GiB pool)", limit.Value(), want)
	}

	// Off, nothing is bounded — every other worker in the fleet renders as it did before.
	in.Deployment.Serving.PrefixCacheConfig = nil
	spec, err = podSpec(in, Options{ModelCachePVC: "model-cache"}, workerRole{Role: "prefill"})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := spec.Containers[0].Resources.Limits[corev1.ResourceMemory]; ok {
		t.Error("memory limit rendered with the tier off; that is a fleet-wide OOMKill risk for no gain")
	}
}
