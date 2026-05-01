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

	// MultinodeEnabled says whether this cluster can run a replica that spans machines.
	//
	// A per-cluster fact rather than a plane decision, and a hard one: a multi-node replica is a
	// leader-worker group, and Dynamo renders one only where Grove or LeaderWorkerSet is
	// installed. Without them the CRD accepts the field and produces a single pod asking for a
	// fraction of the devices the engine was told to expect, which starts and then hangs in
	// collective initialisation. Refusing is the only honest behaviour, so this defaults off and
	// a cluster turns it on when it has installed the thing that makes it true.
	MultinodeEnabled bool
}

// Inputs is everything one render reads. Grouping them makes the dependency explicit: a
// deployment alone renders nothing, because how to serve a model is on its recipe and where the
// weights come from is on its artifact.
type Inputs struct {
	Deployment wire.DeploymentDesired
	Recipe     wire.RecipeDesired
	Artifact   wire.ArtifactDesired
	Layout     Layout
}

// Layout is how one replica's devices are spread over machines, as the capacity check concluded.
//
// Passed in rather than re-derived here, because what was admitted and what is applied have to be
// the one calculation. A zero value means the ordinary case — the whole replica on one node — so
// a caller that has not run the check still renders what it always did.
type Layout struct {
	// DevicesPerNode is what one pod asks for.
	DevicesPerNode int
	// NodesPerReplica is how many pods one replica is.
	NodesPerReplica int
	// Domain is the fabric every pod of the replica must land in, empty when unpinned.
	Domain string
}

// nodes is how many machines one replica spans, never less than one.
func (l Layout) nodes() int {
	if l.NodesPerReplica < 1 {
		return 1
	}
	return l.NodesPerReplica
}

// devices is what one pod asks the scheduler for, falling back to the whole replica.
func (l Layout) devices(dep wire.DeploymentDesired) int {
	if l.DevicesPerNode > 0 {
		return l.DevicesPerNode
	}
	if n := dep.Placement.DevicesPerReplica; n > 0 {
		return n
	}
	return 1
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

	component := map[string]any{
		"name":                  workerComponent,
		"type":                  "decode",
		"replicas":              int64(Replicas(in.Deployment)),
		"globalDynamoNamespace": true,
		"podTemplate": map[string]any{
			"metadata": map[string]any{"labels": ownershipLabels(in)},
			"spec":     podMap,
		},
	}
	// A replica that spans machines is one leader-worker group, not several replicas. Dynamo
	// renders it from the same pod template, which is why the device request above is per node
	// and not per replica.
	if n := in.Layout.nodes(); n > 1 {
		component["multinode"] = map[string]any{"nodeCount": int64(n)}
	}

	obj := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": GVK.GroupVersion().String(),
		"kind":       GVK.Kind,
		"metadata": map[string]any{
			"name":      WorkloadName(in.Deployment),
			"namespace": opts.Namespace,
			"labels":    ownershipLabels(in),
			"annotations": map[string]any{
				"hangar.dev/generation":      strconv.FormatInt(in.Deployment.Generation, 10),
				"hangar.dev/recipe-revision": strconv.Itoa(in.Recipe.Version),
			},
		},
		"spec": map[string]any{
			"components": []any{component},
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

// maxWorkloadName bounds the rendered name so the objects Dynamo derives from it stay inside the
// 63 byte limit their own names are held to.
//
// Derived rather than chosen. The number that matters is not the graph deployment's own name — it
// has 253 bytes to spend — but the DynamoComponentDeployment Dynamo names after it, whose name is
// also the value of the `nvidia.com/selector` label on the Deployment underneath. A label value is
// 63 bytes, so a graph name that fits perfectly well produces a child the API server refuses:
//
//	Deployment.apps "<40 chars>-vllmdecodeworker-bb8c5a8c" is invalid:
//	spec.selector.matchLabels: must be no more than 63 bytes
//
// A constant 40 was three over, and the failure landed on an object this operator does not own and
// never read — so the deployment reported `placed · 0 of 1 replicas ready` for as long as it
// existed, with no pod ever created and nothing anywhere saying why.
const maxWorkloadName = maxObjectNameBytes - len(componentSuffix)

// maxObjectNameBytes is the label-value limit the derived name is actually held to.
const maxObjectNameBytes = 63

// componentSuffix is what Dynamo appends to the graph's name to name one component's objects:
// a dash, the component lowercased, a dash, and an eight character spec hash.
//
// Written out rather than lowercased from `workerComponent`, because a const has to be a const.
// `TestComponentSuffixMatchesWorker` is what keeps the two from drifting.
const componentSuffix = "-vllmdecodeworker-" + specHashPlaceholder

// specHashPlaceholder stands in for the hash Dynamo derives per component. Only its length is
// used; the value is never rendered.
const specHashPlaceholder = "00000000"

// WorkloadName is the object name one deployment renders under.
//
// The deployment's own name rather than its id, because every Kubernetes object underneath is
// named after this one: the id put `dep-01m0sktt1s3vszhp5rkdp925dh-vllmdecodeworker-...` on every
// pod, which tells a person reading `kubectl get pods` nothing they came to find out. The name is
// unique per cluster, which is the property that makes it usable as an object name at all.
//
// The id remains the fallback for a deployment that names itself nothing renderable, because a
// workload with no name is still a workload that has to be applied somewhere.
func WorkloadName(dep wire.DeploymentDesired) string {
	if slug := slugify(dep.Name); slug != "" {
		return slug
	}
	return Name(dep.DeploymentID)
}

// slugify reduces a human's name to something RFC 1123 accepts, or to empty when nothing survives.
func slugify(in string) string {
	var b strings.Builder
	dash := false
	for _, r := range strings.ToLower(in) {
		switch {
		case (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9'):
			b.WriteRune(r)
			dash = false
		case b.Len() > 0 && !dash:
			b.WriteByte('-')
			dash = true
		}
	}
	out := strings.Trim(b.String(), "-")
	if len(out) > maxWorkloadName {
		out = strings.Trim(out[:maxWorkloadName], "-")
	}
	return out
}

// ownershipLabels mark everything this operator renders for a deployment as ours.
//
// They go on the pod template as well as on the graph deployment, and that is what makes them
// load-bearing rather than decorative: discovery reads pod labels to decide whether a running
// workload is one the plane put there, and Dynamo does not copy a graph deployment's labels onto
// the pods it derives from it. Without them on the template, every deployment this operator
// created came back up the wire as a sighting to be adopted.
func ownershipLabels(in Inputs) map[string]any {
	return map[string]any{
		"hangar.dev/deployment-id": in.Deployment.DeploymentID,
		"hangar.dev/recipe":        in.Recipe.RecipeID,
		"hangar.dev/project":       in.Deployment.ProjectID,
		"hangar.dev/managed-by":    "hangar-operator",
	}
}

// Replicas is what the deployment asks to run right now.
//
// The number is this cluster's share of a total the operator never sees. A workload declares a
// total across every cluster it is allowed to use and the plane divides it; a deployment carries
// one portion of that division. This operator sees one cluster and cannot tell a whole workload
// from a fragment of one, which is the point — it renders exactly the number it is sent, and must
// never sum, scale, or re-derive it against anything else.
//
// MinReplicas, not MaxReplicas: the maximum is the ceiling an autoscaler is allowed to reach, and
// nothing in this operator autoscales yet. Rendering the ceiling would run every deployment at its
// most expensive configuration from the moment it is created.
//
// Exported because the reconciler compares what is running against it. Two definitions of how
// many replicas ought to exist would eventually disagree, and the one that decides what is
// rendered has to be the one that decides whether the result is complete.
func Replicas(dep wire.DeploymentDesired) int {
	if dep.MinReplicas < 0 {
		return 0
	}
	return dep.MinReplicas
}

func podSpec(in Inputs, opts Options) (*corev1.PodSpec, error) {
	devices := in.Layout.devices(in.Deployment)

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
		NodeSelector: podNodeSelector(in, opts),
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

// podNodeSelector is what the render actually lands on: the deployment's selector, plus the fabric
// when the replica was pinned to one.
//
// Kept apart from NodeSelector because the capacity check runs before a domain has been chosen and
// has to ask about every node, where the render asks about the one domain the check settled on.
func podNodeSelector(in Inputs, opts Options) map[string]string {
	out := NodeSelector(in.Deployment, opts)
	if kind, key, ok := splitDomain(in.Layout.Domain); ok && kind == "nvlink" {
		out[CliqueLabel] = key
	}
	return out
}

// CliqueLabel names the fabric a node's accelerators belong to. Exported because the capacity
// check groups nodes by the same label the render selects on; two spellings of one fact is how
// they drift apart.
const CliqueLabel = "nvidia.com/gpu.clique"

// splitDomain takes a domain id apart into what is shared and which one it is.
func splitDomain(domain string) (kind, key string, ok bool) {
	slash := strings.Index(domain, "/")
	if slash < 1 || slash == len(domain)-1 {
		return "", "", false
	}
	return domain[:slash], domain[slash+1:], true
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

// Label keys that record which declared object a rendered one came from.
//
// They exist because an owner reference often cannot. Kubernetes resolves an ownerReference in the
// dependent's own namespace, so a namespaced owner in a different namespace does not merely fail
// to link the two — the garbage collector reads it as an owner that has been deleted and removes
// the dependent, seconds after it was applied. Declared state lives in Options.Namespace and
// rendered workloads live in the workload namespace, so that is the ordinary case here, not an
// edge one.
const (
	OwnerNameLabel      = "hangar.dev/owner"
	OwnerNamespaceLabel = "hangar.dev/owner-namespace"
)

// Owned records which declared object a rendered one came from.
//
// An owner reference is stamped only when the two share a namespace, where it is both valid and
// useful. Across namespaces the labels are the only link, and teardown becomes a real code path —
// the finalizer in the reconciler — rather than the API server's job. That is the cost of
// rendering into a namespace of its own, and it is paid explicitly here instead of being written
// as a reference that Kubernetes will act on in the one way nobody wants.
func Owned(obj *unstructured.Unstructured, owner metav1.Object, gvk schema.GroupVersionKind) {
	OwnedMeta(obj, owner)

	if obj.GetNamespace() != owner.GetNamespace() {
		obj.SetOwnerReferences(nil)
		return
	}
	obj.SetOwnerReferences([]metav1.OwnerReference{{
		APIVersion:         gvk.GroupVersion().String(),
		Kind:               gvk.Kind,
		Name:               owner.GetName(),
		UID:                owner.GetUID(),
		Controller:         ptr(true),
		BlockOwnerDeletion: ptr(true),
	}})
}

// OwnedMeta stamps the owner labels alone, for a rendered object that never takes an owner
// reference — a prefetch Job, whose pod template is immutable and which is therefore created once
// and left alone rather than applied on every pass.
func OwnedMeta(obj metav1.Object, owner metav1.Object) {
	labels := obj.GetLabels()
	if labels == nil {
		labels = map[string]string{}
	}
	labels[OwnerNameLabel] = owner.GetName()
	labels[OwnerNamespaceLabel] = owner.GetNamespace()
	obj.SetLabels(labels)
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
