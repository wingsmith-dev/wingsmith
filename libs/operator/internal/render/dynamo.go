// Package render turns declared domain objects into the Kubernetes objects that serve them.
//
// This is the only package in the operator that knows what a DynamoGraphDeployment is, and the
// only one that knows what a vLLM flag looks like. Everything upstream of it deals in the domain
// — recipes, deployments, artifacts — which is rule 2 of the design: the wire format is our
// objects, and the mapping to Kubernetes lives here and nowhere else.
//
// The rendered object is built as unstructured rather than against NVIDIA's Go types. Vendoring
// their API module to write four nested fields would tie this operator's build to their release
// cadence for no benefit, and the object is applied server-side anyway, where the API server does
// the validating.
package render

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/strategicpatch"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// GVK is the Dynamo graph deployment this operator renders into.
var GVK = schema.GroupVersionKind{Group: "nvidia.com", Version: "v1beta1", Kind: "DynamoGraphDeployment"}

// Options are the per-cluster conventions the render needs and the plane does not declare.
type Options struct {
	Namespace           string
	PoolLabelKey        string
	GPUPoolValue        string
	CachePoolValue      string
	CacheTaintKey       string
	ModelCachePVC       string
	PrefetchCPU         string
	PrefetchMemory      string
	PrefetchMemoryLimit string
}

// Inputs is everything one render reads. Grouping them makes the dependency explicit: a
// deployment alone renders nothing, because how to serve a model is on its recipe and where the
// weights come from is on its artifact.
type Inputs struct {
	Deployment wire.DeploymentDesired
	Recipe     wire.RecipeDesired
	Artifact   wire.ArtifactDesired
}

// workerComponent is the Dynamo component name for the engine.
//
// One worker, aggregated: prefill and decode share an engine loop. Disaggregating them is a
// topology decision with a real trade — it buys latency isolation and pays for it in KV capacity,
// because each worker loads its own copy of the weights — and nothing on the recipe expresses that
// choice yet. Rendering it silently would be picking a side of that trade on the operator's own
// authority.
const workerComponent = "VllmDecodeWorker"

// DynamoGraphDeployment renders one deployment into the object that serves it.
//
// No Frontend component is rendered. A cluster runs exactly one shared frontend across every
// model on it, because the model name lives in the request body where no ingress can route on it,
// so a frontend per deployment would leave nothing able to choose between them. The workers here
// join that shared frontend through globalDynamoNamespace.
func DynamoGraphDeployment(in Inputs, opts Options) (*unstructured.Unstructured, error) {
	pod, err := podSpec(in, opts)
	if err != nil {
		return nil, err
	}
	podMap, err := runtime.DefaultUnstructuredConverter.ToUnstructured(pod)
	if err != nil {
		return nil, fmt.Errorf("convert pod spec: %w", err)
	}

	obj := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": GVK.GroupVersion().String(),
		"kind":       GVK.Kind,
		"metadata": map[string]any{
			"name":      Name(in.Deployment.DeploymentID),
			"namespace": opts.Namespace,
			"labels": map[string]any{
				"hangar.dev/deployment": in.Deployment.DeploymentID,
				"hangar.dev/recipe":     in.Recipe.RecipeID,
				"hangar.dev/project":    in.Deployment.ProjectID,
				"hangar.dev/managed-by": "hangar-operator",
			},
			"annotations": map[string]any{
				"hangar.dev/generation":      strconv.FormatInt(in.Deployment.Generation, 10),
				"hangar.dev/recipe-revision": strconv.Itoa(in.Recipe.Version),
			},
		},
		"spec": map[string]any{
			"components": []any{
				map[string]any{
					"name":                  workerComponent,
					"type":                  "decode",
					"replicas":              int64(replicas(in.Deployment)),
					"globalDynamoNamespace": true,
					"podTemplate":           map[string]any{"spec": podMap},
				},
			},
		},
	}}

	if err := applyDynamoOverlay(obj, in.Recipe.Overlay.Dynamo); err != nil {
		return nil, err
	}
	return obj, nil
}

// Name renders a prefixed entity id as a Kubernetes object name.
func Name(id string) string {
	return strings.ToLower(strings.ReplaceAll(id, "_", "-"))
}

// replicas is what the deployment asks to run right now.
//
// MinReplicas, not MaxReplicas: the maximum is the ceiling an autoscaler is allowed to reach, and
// nothing in this operator autoscales yet. Rendering the ceiling would run every deployment at its
// most expensive configuration from the moment it is created.
func replicas(dep wire.DeploymentDesired) int {
	if dep.MinReplicas < 0 {
		return 0
	}
	return dep.MinReplicas
}

func podSpec(in Inputs, opts Options) (*corev1.PodSpec, error) {
	devices := in.Deployment.Placement.DevicesPerReplica
	if devices < 1 {
		devices = 1
	}

	requests := corev1.ResourceList{"nvidia.com/gpu": *resource.NewQuantity(int64(devices), resource.DecimalSI)}
	limits := corev1.ResourceList{"nvidia.com/gpu": *resource.NewQuantity(int64(devices), resource.DecimalSI)}
	if n := in.Recipe.Resources.CPUCores; n != nil {
		requests[corev1.ResourceCPU] = *resource.NewQuantity(int64(*n), resource.DecimalSI)
	}
	if n := in.Recipe.Resources.MemoryMiB; n != nil {
		requests[corev1.ResourceMemory] = *resource.NewQuantity(*n*1024*1024, resource.BinarySI)
	}
	if n := in.Recipe.Resources.DiskGiB; n != nil {
		requests[corev1.ResourceEphemeralStorage] = *resource.NewQuantity(int64(*n)*1024*1024*1024, resource.BinarySI)
	}

	image := ""
	if in.Recipe.EngineImage != nil {
		image = *in.Recipe.EngineImage
	}
	if image == "" {
		return nil, fmt.Errorf("recipe %s names no engine image", in.Recipe.RecipeID)
	}

	args, err := engineArgs(in)
	if err != nil {
		return nil, err
	}

	// Tensor-parallel workers exchange activations over shared memory. The container default of
	// 64MB is far too small, and the failure is a NCCL or bus error at startup that never names
	// /dev/shm as the cause — so it is sized here rather than left to be rediscovered.
	shm := resource.MustParse("16Gi")

	spec := &corev1.PodSpec{
		NodeSelector: NodeSelector(in.Deployment, opts),
		Tolerations: []corev1.Toleration{{
			Key:      "nvidia.com/gpu",
			Operator: corev1.TolerationOpEqual,
			Value:    "true",
			Effect:   corev1.TaintEffectNoSchedule,
		}},
		Volumes: []corev1.Volume{
			{
				Name: "model-cache",
				VolumeSource: corev1.VolumeSource{
					PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: opts.ModelCachePVC},
				},
			},
			{
				Name: "dshm",
				VolumeSource: corev1.VolumeSource{
					EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory, SizeLimit: &shm},
				},
			},
		},
		Containers: []corev1.Container{{
			Name:       "main",
			Image:      image,
			WorkingDir: "/workspace/examples/backends/vllm",
			Command:    []string{"python3", "-m", "dynamo." + engineModule(in.Recipe.Engine)},
			Args:       args,
			Env: []corev1.EnvVar{
				{Name: "HF_HOME", Value: "/models/hf"},
				// KV-aware routing decides which worker already holds a prefix by hashing
				// block contents. Python salts hashes per process by default, so without a
				// fixed seed the frontend and the workers hash identical blocks differently
				// and every cache hit is missed — silently, as a performance loss.
				{Name: "PYTHONHASHSEED", Value: "0"},
			},
			EnvFrom: []corev1.EnvFromSource{{
				SecretRef: &corev1.SecretEnvSource{
					LocalObjectReference: corev1.LocalObjectReference{Name: "hf-token-secret"},
					Optional:             ptr(true),
				},
			}},
			VolumeMounts: []corev1.VolumeMount{
				{Name: "model-cache", MountPath: "/models"},
				{Name: "dshm", MountPath: "/dev/shm"},
			},
			Resources: corev1.ResourceRequirements{Requests: requests, Limits: limits},
		}},
	}

	return applyPodOverlay(spec, in.Recipe.Overlay.PodSpec)
}

// NodeSelector puts the replica on accelerator nodes, plus whatever the plane constrained.
//
// Exported because the capacity precondition has to ask about the same nodes this render will
// land on. A check run against a wider set than the render targets is not a check.
func NodeSelector(dep wire.DeploymentDesired, opts Options) map[string]string {
	out := map[string]string{opts.PoolLabelKey: opts.GPUPoolValue}
	for key, value := range dep.Placement.NodeSelector {
		out[key] = value
	}
	return out
}

// engineModule maps a recipe engine onto the Dynamo entrypoint that runs it.
func engineModule(engine string) string {
	switch strings.ToLower(engine) {
	case "sglang":
		return "sglang"
	case "trtllm", "tensorrt-llm", "tensorrt_llm":
		return "trtllm"
	default:
		return "vllm"
	}
}

// engineArgs builds the engine command line from the recipe.
//
// Recipe fields are rendered first and engine_args second, so an explicit argument wins over the
// derived one. That direction is deliberate: engine_args is the escape hatch for a flag this
// operator has never heard of, and an escape hatch that loses to a default is not one.
func engineArgs(in Inputs) ([]string, error) {
	model, err := modelRef(in.Artifact)
	if err != nil {
		return nil, err
	}

	derived := map[string]string{
		"model":             model,
		"served-model-name": in.Recipe.Name,
		"max-model-len":     strconv.Itoa(in.Recipe.ContextLength),
	}
	if rev := hfRevision(in.Artifact); rev != "" {
		derived["revision"] = rev
	}
	if tp := in.Recipe.Parallelism.Tensor; tp > 1 {
		derived["tensor-parallel-size"] = strconv.Itoa(tp)
	}
	if pp := in.Recipe.Parallelism.Pipeline; pp > 1 {
		derived["pipeline-parallel-size"] = strconv.Itoa(pp)
	}
	if f := in.Recipe.KVCacheFraction; f != nil {
		derived["gpu-memory-utilization"] = strconv.FormatFloat(*f, 'f', -1, 64)
	}
	if v := in.Recipe.Quantization; v != nil && *v != "" {
		derived["quantization"] = *v
	}
	if v := in.Recipe.Dtype; v != nil && *v != "" {
		derived["dtype"] = *v
	}
	if v := in.Recipe.KVCacheDtype; v != nil && *v != "" {
		derived["kv-cache-dtype"] = *v
	}
	if v := in.Recipe.MaxBatchTokens; v != nil {
		derived["max-num-batched-tokens"] = strconv.Itoa(*v)
	}
	for key, value := range in.Recipe.EngineArgs {
		derived[strings.TrimPrefix(key, "--")] = value
	}

	keys := make([]string, 0, len(derived))
	for key := range derived {
		keys = append(keys, key)
	}
	sort.Strings(keys)

	args := make([]string, 0, len(keys)*2+1)
	for _, key := range keys {
		args = append(args, "--"+key)
		if derived[key] != "" {
			args = append(args, derived[key])
		}
	}
	// vLLM enables prefix caching by default, so only the off case needs saying.
	if !in.Recipe.PrefixCacheEnabled {
		args = append(args, "--no-enable-prefix-caching")
	}
	return args, nil
}

// modelRef is what --model is pointed at.
//
// A Hugging Face source is preferred over the object-store one because the engine resolves a repo
// id against HF_HOME, which is the shared cache volume: the weights are pulled once per cluster
// rather than once per replica. An artifact with no HF source falls back to the digest path the
// cache writes to.
func modelRef(art wire.ArtifactDesired) (string, error) {
	best := ""
	for _, source := range art.Sources {
		if strings.HasPrefix(source.URI, "hf://") {
			if best == "" {
				best, _ = SplitHFRef(source.URI)
			}
		}
	}
	if best != "" {
		return best, nil
	}
	if art.ArtifactID == "" {
		return "", fmt.Errorf("no artifact to serve")
	}
	return "/models/" + art.ArtifactID, nil
}

// applyPodOverlay merges recipe-supplied pod configuration over the rendered spec.
//
// Strategic merge rather than a plain JSON merge patch, because PodSpec declares merge keys:
// containers, env, volumes and volumeMounts merge by name. Under RFC 7386 those are lists and a
// list is replaced wholesale, so adding one environment variable would mean restating every one
// this renderer set — including PYTHONHASHSEED, whose absence costs prefix cache hits and reports
// nothing.
func applyPodOverlay(spec *corev1.PodSpec, overlay map[string]any) (*corev1.PodSpec, error) {
	if len(overlay) == 0 {
		return spec, nil
	}
	original, err := json.Marshal(spec)
	if err != nil {
		return nil, fmt.Errorf("marshal pod spec: %w", err)
	}
	patch, err := json.Marshal(overlay)
	if err != nil {
		return nil, fmt.Errorf("marshal pod overlay: %w", err)
	}
	merged, err := strategicpatch.StrategicMergePatch(original, patch, corev1.PodSpec{})
	if err != nil {
		return nil, fmt.Errorf("apply pod overlay: %w", err)
	}
	var out corev1.PodSpec
	if err := json.Unmarshal(merged, &out); err != nil {
		return nil, fmt.Errorf("decode merged pod spec: %w", err)
	}
	return &out, nil
}

// applyDynamoOverlay merges recipe-supplied configuration over the rendered graph deployment.
//
// A plain recursive merge, and it has to be: DynamoGraphDeployment is someone else's CRD, so
// there is no Go type here to read merge keys off. Maps merge, and anything else — including
// lists — replaces. Replacing spec.components wholesale is the intended way to hand-write a
// disaggregated topology this renderer does not know how to derive.
func applyDynamoOverlay(obj *unstructured.Unstructured, overlay map[string]any) error {
	if len(overlay) == 0 {
		return nil
	}
	spec, ok := obj.Object["spec"].(map[string]any)
	if !ok {
		return fmt.Errorf("rendered object has no spec")
	}
	obj.Object["spec"] = mergeMaps(spec, overlay)
	return nil
}

func mergeMaps(base, over map[string]any) map[string]any {
	out := make(map[string]any, len(base)+len(over))
	for key, value := range base {
		out[key] = value
	}
	for key, value := range over {
		existing, hasExisting := out[key]
		baseMap, baseIsMap := existing.(map[string]any)
		overMap, overIsMap := value.(map[string]any)
		if hasExisting && baseIsMap && overIsMap {
			out[key] = mergeMaps(baseMap, overMap)
			continue
		}
		out[key] = value
	}
	return out
}

// Owned stamps an owner reference so that deleting the declared copy garbage-collects what was
// rendered from it. Teardown is therefore the API server's job and not a code path here — there
// is no delete branch in the reconciler to get wrong.
func Owned(obj *unstructured.Unstructured, owner metav1.Object, gvk schema.GroupVersionKind) {
	obj.SetOwnerReferences([]metav1.OwnerReference{{
		APIVersion:         gvk.GroupVersion().String(),
		Kind:               gvk.Kind,
		Name:               owner.GetName(),
		UID:                owner.GetUID(),
		Controller:         ptr(true),
		BlockOwnerDeletion: ptr(true),
	}})
}

func ptr[T any](v T) *T { return &v }

// SplitHFRef separates a HuggingFace URI into the repo id and the revision pinned onto it.
//
// The plane writes a pinned artifact as hf://owner/name@<sha>, and huggingface_hub validates the
// repo id against a character set that does not include '@' — so passing the whole thing through
// fails the pull outright rather than resolving the pin. The separator is the last '@' because a
// repo id may not contain one and a revision may not either, but splitting from the right is the
// reading that survives a source that acquires one later.
func SplitHFRef(uri string) (repo, revision string) {
	repo = strings.TrimPrefix(uri, "hf://")
	if i := strings.LastIndex(repo, "@"); i >= 0 {
		return repo[:i], repo[i+1:]
	}
	return repo, ""
}

// hfRevision is the revision the preferred HuggingFace source pins, if it pins one.
//
// Handed to the engine as --revision so that it resolves the same commit the prefetch downloaded.
// Without it the engine resolves the branch head against the shared cache, which is a second copy
// of the weights at best and a different model at worst.
func hfRevision(art wire.ArtifactDesired) string {
	for _, source := range art.Sources {
		if strings.HasPrefix(source.URI, "hf://") {
			_, revision := SplitHFRef(source.URI)
			return revision
		}
	}
	return ""
}
