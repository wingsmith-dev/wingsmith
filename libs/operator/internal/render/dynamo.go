// Package render turns declared domain objects into the Kubernetes objects that serve them.
//
// This is the only package in the operator that knows what a DynamoGraphDeployment is, and the
// only one that knows what a vLLM flag looks like. Everything upstream of it deals in the domain
// — deployments, their serving configuration, artifacts — which is rule 2 of the design: the wire
// format is our objects, and the mapping to Kubernetes lives here and nowhere else.
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

	// FrontendEnabled renders a frontend beside the worker, one per model.
	//
	// Off by default, and that default is the migration control rather than an opinion. Every
	// cluster we run today is served by one frontend the chart installs, and with this false the
	// rendered object is byte-identical to what that arrangement has always produced — so the
	// operator can ship before any cluster has moved, and a cluster moves by setting one value.
	// It is the only flag here whose false case has to stay exactly as it was.
	FrontendEnabled bool

	// FrontendImage is this cluster's default frontend runtime, used when the deployment names
	// none.
	//
	// A per-cluster fact for the same reason the pool labels are: the plane says which model to
	// serve, and which build of the frontend a cluster happens to run is local. Empty falls back
	// to the serving block's engine image, which is right often enough — the Dynamo runtime
	// carries the frontend and the engines in one image — and wrong exactly when the frontend
	// needs a patch the engine does not, which is what ServingSpec.FrontendImage exists for.
	FrontendImage string

	// FrontendReplicas is how many frontend pods one model gets. Two by default, so that a
	// rollout does not take the model's only frontend with it: the frontend holds no weights, so
	// a second replica costs a few hundred megabytes and buys the model an upgrade that is not
	// an outage.
	FrontendReplicas int

	// FrontendPullSecrets are Secrets in the workload namespace the kubelet presents when
	// pulling FrontendImage.
	//
	// The worker path sets none and has never needed any, because an engine image comes from
	// nvcr.io, which serves anonymously. A frontend image does not have to: the Kimi K3 fix is a
	// patched runtime in a private GHCR repository, and a private pull with no credential is an
	// ImagePullBackOff rather than an error anything reports upward. A pull Secret is namespaced
	// and the kubelet reads it from the pod's own namespace, so these name Secrets in the
	// namespace this operator renders into, not in its own.
	FrontendPullSecrets []string

	// FrontendCPU, FrontendMemory and FrontendMemoryLimit are what one frontend pod asks for.
	// Empty takes the render's own defaults, which are what the cluster frontend has been
	// observed to run inside. No CPU limit is ever set: the frontend holds streams open and
	// copies every token of every response, so throttling it is latency on the data path.
	FrontendCPU         string
	FrontendMemory      string
	FrontendMemoryLimit string

	// AnthropicAPIEnabled serves the Anthropic Messages API (`/v1/messages`) beside the OpenAI
	// one, and AnthropicStripPreamble drops the Claude Code billing preamble from the system
	// prompt. Both are additive — a route that did not exist before, and a prompt that starts
	// the same way every time, which is a prompt the prefix cache can hit — and both are set as
	// environment rather than arguments because DynamoGraphDeployment owns the command line.
	AnthropicAPIEnabled    bool
	AnthropicStripPreamble bool

	// FrontendGlobalNamespace sets globalDynamoNamespace on the frontend component. Nil means
	// true, which is today's behaviour on every cluster: the chart's frontend and every worker
	// this operator renders both run with it on, and both pods carry DYN_NAMESPACE=dynamo, so
	// everything on a cluster discovers everything else.
	//
	// It is a setting and not a constant because whether the namespace can be narrowed — a
	// frontend that sees only its own model's workers — is unresolved and cannot be settled by
	// reading anything. It needs an experiment on a live cluster: whether a worker registered in
	// a per-deployment namespace is discovered by a second frontend at all, and whether two
	// frontends in one namespace both attempt registration of every model. The point of the
	// field is that the answer becomes one value on one cluster rather than a code change.
	FrontendGlobalNamespace *bool
}

// Inputs is everything one render reads. Grouping them makes the dependency explicit: a
// deployment carries how to serve the model on its own serving block, and where the weights come
// from is on its artifact.
type Inputs struct {
	Deployment wire.DeploymentDesired
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

// WorkerComponent is the Dynamo component name for the engine.
//
// One worker, aggregated: prefill and decode share an engine loop. Disaggregating them is a
// topology decision with a real trade — it buys latency isolation and pays for it in KV capacity,
// because each worker loads its own copy of the weights — and nothing on the serving block
// expresses that choice yet. Rendering it silently would be picking a side of that trade on the operator's own
// authority.
const WorkerComponent = "VllmDecodeWorker"

// FrontendComponent is the Dynamo component name for the OpenAI-compatible frontend.
//
// The string is not ours to choose. Dynamo puts a component's name in the `nvidia.com/dynamo-
// component` label and in the Service selector derived from it, and two things in this repository
// find a frontend by that label rather than by name — the serving agent's resolver and this
// operator's own benchmark executor — because the name is the part that varies between clusters
// while the label does not. Renaming this breaks both, and it breaks them as a lookup that finds
// nothing rather than as a compile error.
const FrontendComponent = "Frontend"

// DynamoGraphDeployment renders one deployment into the object that serves it.
//
// The graph holds the model's worker and, where the cluster has opted in, the frontend that model
// is served through. One frontend per model rather than one per cluster, because a frontend is a
// binary and that binary has to tokenize and register every model reaching it: on 2026-09-01 a
// model arrived on site-c that the cluster's frontend could not tokenize, and the only way to serve
// it was to change the image every other model on that cluster was served through. A frontend per
// model turns a fleet event into one deployment's business.
//
// Nothing has to route on the request body for this to work, which is what the earlier argument
// for the singleton assumed. The gateway has already resolved which deployment a request belongs
// to before it forwards it, so it forwards that deployment id and the in-cluster agent picks the
// frontend by label. No frontend is chosen by asking anything what models it serves.
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
		"name":                  WorkerComponent,
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

	components := []any{component}
	if opts.FrontendEnabled {
		frontend, err := frontendComponent(in, opts)
		if err != nil {
			return nil, err
		}
		components = append(components, frontend)
	}

	obj := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": GVK.GroupVersion().String(),
		"kind":       GVK.Kind,
		"metadata": map[string]any{
			"name":      WorkloadName(in.Deployment),
			"namespace": opts.Namespace,
			"labels":    ownershipLabels(in),
			"annotations": map[string]any{
				"hangar.dev/generation":     strconv.FormatInt(in.Deployment.Generation, 10),
				"hangar.dev/serving-digest": in.Deployment.Serving.Digest,
			},
		},
		"spec": map[string]any{
			"components": components,
		},
	}}

	if err := applyDynamoOverlay(obj, in.Deployment.Serving.Overlay.Dynamo); err != nil {
		return nil, err
	}
	if opts.FrontendEnabled {
		if err := checkFrontendSurvivedOverlay(obj, in); err != nil {
			return nil, err
		}
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
//
// The bound is the longest of the components actually rendered, and not the worker's alone. The
// worker's is the longest today — `Frontend` is half the length of `VllmDecodeWorker`, so the
// number does not move and no existing name changes — but a bound derived from one component is a
// bound that is right by coincidence. A component renamed or added past it would overflow the
// same 63 bytes, in the same place, with the same silence.
const maxWorkloadName = maxObjectNameBytes - longestComponentSuffix

// longestComponentSuffix is the worst case over every component this renderer emits.
const longestComponentSuffix = max(len(componentSuffix), len(frontendSuffix))

// maxObjectNameBytes is the label-value limit the derived name is actually held to.
const maxObjectNameBytes = 63

// componentSuffix is what Dynamo appends to the graph's name to name one component's objects:
// a dash, the component lowercased, a dash, and an eight character spec hash.
//
// Written out rather than lowercased from `WorkerComponent`, because a const has to be a const.
// `TestComponentSuffixMatchesWorker` is what keeps the two from drifting.
const componentSuffix = "-vllmdecodeworker-" + specHashPlaceholder

// frontendSuffix is the same for the frontend component. `TestFrontendSuffixMatchesTheComponent`
// is what keeps this one from drifting.
const frontendSuffix = "-frontend-" + specHashPlaceholder

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

// deploymentIDLabel is the ownership label that says which deployment an object belongs to, and
// the one of the four that is also a routing key: Dynamo copies a component's pod-template labels
// onto the Service it derives, and this is what the in-cluster agent matches to pick a frontend
// for a request the gateway has already resolved to this deployment.
const deploymentIDLabel = "hangar.dev/deployment-id"

// ownershipLabels mark everything this operator renders for a deployment as ours.
//
// They go on the pod template as well as on the graph deployment, and that is what makes them
// load-bearing rather than decorative: discovery reads pod labels to decide whether a running
// workload is one the plane put there, and Dynamo does not copy a graph deployment's labels onto
// the pods it derives from it. Without them on the template, every deployment this operator
// created came back up the wire as a sighting to be adopted.
func ownershipLabels(in Inputs) map[string]any {
	return map[string]any{
		deploymentIDLabel:       in.Deployment.DeploymentID,
		"hangar.dev/project":    in.Deployment.ProjectID,
		"hangar.dev/managed-by": "hangar-operator",
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
	if n := in.Deployment.Serving.Resources.CPUCores; n != nil {
		requests[corev1.ResourceCPU] = *resource.NewQuantity(int64(*n), resource.DecimalSI)
	}
	if n := in.Deployment.Serving.Resources.MemoryMiB; n != nil {
		requests[corev1.ResourceMemory] = *resource.NewQuantity(*n*1024*1024, resource.BinarySI)
	}
	if n := in.Deployment.Serving.Resources.DiskGiB; n != nil {
		requests[corev1.ResourceEphemeralStorage] = *resource.NewQuantity(int64(*n)*1024*1024*1024, resource.BinarySI)
	}

	image := ""
	if in.Deployment.Serving.EngineImage != nil {
		image = *in.Deployment.Serving.EngineImage
	}
	if image == "" {
		return nil, fmt.Errorf("deployment %s names no engine image", in.Deployment.DeploymentID)
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
			Command:    []string{"python3", "-m", "dynamo." + engineModule(in.Deployment.Serving.Engine)},
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

	return applyPodOverlay(spec, in.Deployment.Serving.Overlay.PodSpec)
}

// Defaults for the frontend pod, matching what the cluster frontend has been observed to run
// inside. They are small because the frontend holds no weights: it routes, and what it keeps on
// disk is one deployment card and tokenizer per model it serves.
const (
	defaultFrontendReplicas    = 2
	defaultFrontendCPU         = "200m"
	defaultFrontendMemory      = "512Mi"
	defaultFrontendMemoryLimit = "2Gi"
)

// frontendComponent renders the frontend this model is served through.
//
// The pod template carries ownershipLabels, and that is the load-bearing part rather than the
// bookkeeping it looks like. Dynamo copies a component's pod-template labels onto the Service it
// derives — verified on site-c, where the worker Service carries every one of them — and
// `hangar.dev/deployment-id` on that Service is the entire key by which the in-cluster agent
// picks this frontend for a request the gateway has already resolved to this deployment. Without
// the labels here the Service is anonymous and the deployment is reachable by nothing.
//
// globalDynamoNamespace is a setting rather than a constant, for the reason on
// Options.FrontendGlobalNamespace: today every frontend and every worker on a cluster share one
// Dynamo namespace, and whether that can be narrowed needs a cluster experiment nobody has run.
func frontendComponent(in Inputs, opts Options) (map[string]any, error) {
	pod, err := frontendPodSpec(in, opts)
	if err != nil {
		return nil, err
	}
	podMap, err := runtime.DefaultUnstructuredConverter.ToUnstructured(pod)
	if err != nil {
		return nil, fmt.Errorf("convert frontend pod spec: %w", err)
	}

	replicas := opts.FrontendReplicas
	if replicas < 1 {
		replicas = defaultFrontendReplicas
	}

	global := true
	if opts.FrontendGlobalNamespace != nil {
		global = *opts.FrontendGlobalNamespace
	}

	return map[string]any{
		// `type: frontend` is what earns the component its ports and the command it runs;
		// `name: Frontend` is what the two label-based lookups match on. Different fields
		// saying different things, and both load-bearing.
		"name":                  FrontendComponent,
		"type":                  "frontend",
		"replicas":              int64(replicas),
		"globalDynamoNamespace": global,
		"podTemplate": map[string]any{
			"metadata": map[string]any{"labels": ownershipLabels(in)},
			"spec":     podMap,
		},
	}, nil
}

// frontendPodSpec is what one frontend pod is.
//
// Deliberately not podSpec. That function puts the accelerator node selector and the
// `nvidia.com/gpu` toleration on everything it renders, which is right for an engine and wrong
// here twice over: a frontend that tolerates the GPU taint schedules onto an accelerator node to
// hold no weights, occupying a machine whose whole value is the cards in it. Left alone it lands
// on ordinary nodes without being told to, because the GPU pool is tainted and this pod says
// nothing about tolerating it.
//
// No command and no arguments either. DynamoGraphDeployment derives the frontend's command line
// from `type: frontend`, so every flag worth setting is set as environment instead — which is why
// the Anthropic options below are `DYN_*` variables and not arguments.
//
// The serving block's pod overlay is deliberately not applied here. It merges by container name and both
// containers are called `main`, so laying it over this one would put the engine's volumes, its
// Hugging Face cache mount and its device requests onto a process that wants none of them — an
// overlay written about the engine, silently applied to something else.
func frontendPodSpec(in Inputs, opts Options) (*corev1.PodSpec, error) {
	image, err := frontendImage(in, opts)
	if err != nil {
		return nil, err
	}
	resources, err := frontendResources(opts)
	if err != nil {
		return nil, err
	}

	var pullSecrets []corev1.LocalObjectReference
	for _, name := range opts.FrontendPullSecrets {
		pullSecrets = append(pullSecrets, corev1.LocalObjectReference{Name: name})
	}

	return &corev1.PodSpec{
		ImagePullSecrets: pullSecrets,
		Containers: []corev1.Container{{
			Name:  "main",
			Image: image,
			Env: []corev1.EnvVar{
				{Name: "DYN_ENABLE_ANTHROPIC_API", Value: strconv.FormatBool(opts.AnthropicAPIEnabled)},
				{Name: "DYN_STRIP_ANTHROPIC_PREAMBLE", Value: strconv.FormatBool(opts.AnthropicStripPreamble)},
				// The same fixed seed the worker gets, and it is worth nothing unless it
				// is the same on both. KV-aware routing decides which worker already
				// holds a prefix by hashing block contents; Python salts hashes per
				// process, so a frontend and a worker that disagree on the salt hash
				// identical blocks differently and miss every cache hit — silently, as a
				// performance loss nothing reports.
				{Name: "PYTHONHASHSEED", Value: "0"},
			},
			Resources: resources,
		}},
	}, nil
}

// frontendImage resolves which runtime the frontend runs, most specific first.
//
// This deployment's own declaration wins because a frontend image is not always the engine image:
// on site-c the Kimi K3 frontend runs a patched build from a private registry while its decode worker
// stays on NVIDIA's stock one, because both defects that made the model unusable are in the
// frontend binary. The cluster default is next, and the engine image is the last resort — right
// often enough, since the Dynamo runtime carries the frontend and the engines together, and the
// only honest answer when nothing else has been said.
func frontendImage(in Inputs, opts Options) (string, error) {
	serving := in.Deployment.Serving
	if serving.FrontendImage != nil {
		if image := strings.TrimSpace(*serving.FrontendImage); image != "" {
			return image, nil
		}
	}
	if image := strings.TrimSpace(opts.FrontendImage); image != "" {
		return image, nil
	}
	if serving.EngineImage != nil {
		if image := strings.TrimSpace(*serving.EngineImage); image != "" {
			return image, nil
		}
	}
	return "", fmt.Errorf(
		"deployment %s names no frontend image and no engine image to fall back on",
		in.Deployment.DeploymentID)
}

// frontendResources is what one frontend pod asks the scheduler for.
//
// A memory ceiling and no CPU one, which is the same trade the chart makes and for the same
// reason: the frontend copies every token of every response, so a CPU limit is latency on the
// data path, while unbounded memory on a pod that downloads model repository files is a node
// nobody can schedule onto.
func frontendResources(opts Options) (corev1.ResourceRequirements, error) {
	cpuText := opts.FrontendCPU
	if cpuText == "" {
		cpuText = defaultFrontendCPU
	}
	memoryText := opts.FrontendMemory
	if memoryText == "" {
		memoryText = defaultFrontendMemory
	}
	ceilingText := opts.FrontendMemoryLimit
	if ceilingText == "" {
		ceilingText = defaultFrontendMemoryLimit
	}

	cpu, err := resource.ParseQuantity(cpuText)
	if err != nil {
		return corev1.ResourceRequirements{}, fmt.Errorf("frontend cpu request %q is not a quantity: %w", cpuText, err)
	}
	memory, err := resource.ParseQuantity(memoryText)
	if err != nil {
		return corev1.ResourceRequirements{}, fmt.Errorf("frontend memory request %q is not a quantity: %w", memoryText, err)
	}
	ceiling, err := resource.ParseQuantity(ceilingText)
	if err != nil {
		return corev1.ResourceRequirements{}, fmt.Errorf("frontend memory limit %q is not a quantity: %w", ceilingText, err)
	}
	if ceiling.Cmp(memory) < 0 {
		return corev1.ResourceRequirements{}, fmt.Errorf("frontend memory limit %s is below the request %s", ceilingText, memoryText)
	}

	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: cpu, corev1.ResourceMemory: memory},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: ceiling},
	}, nil
}

// checkFrontendSurvivedOverlay refuses a render whose overlay left the deployment unreachable.
//
// A refusal, where nothing else about the overlay is refused, and the asymmetry is the point. The
// Dynamo overlay is a recursive map merge over someone else's CRD, so lists replace rather than
// merge — which is the intended way to hand-write a disaggregated topology, and which means an
// overlay that writes `spec.components` at all discards every component this renderer put there.
// Under one frontend per cluster that cost the deployment nothing, because the frontend was not
// in the graph. Now it is, and an overlay written before this change removes the only thing that
// makes the deployment reachable, leaving a graph that applies cleanly, workers that come up
// healthy, and a model no request can arrive at. Nothing in Kubernetes and nothing in Dynamo
// reports that; the only place it can be caught is here.
//
// The routing label is checked and not only the component's presence, because a frontend that is
// there and anonymous fails in exactly the way a missing one does. Dynamo copies a component's
// pod-template labels onto the Service it derives, and `hangar.dev/deployment-id` on that Service
// is how the agent picks this frontend for a request the gateway has already resolved to this
// deployment. A restated component that carries no labels — which is what restating one from the
// example in an error message looks like — is a Service nothing can match, so accepting it would
// let the guard pass a graph with the defect the guard exists for.
func checkFrontendSurvivedOverlay(obj *unstructured.Unstructured, in Inputs) error {
	components, found, err := unstructured.NestedSlice(obj.Object, "spec", "components")
	if err != nil || !found {
		return fmt.Errorf(
			"deployment %s left the rendered object with no spec.components",
			in.Deployment.DeploymentID)
	}
	for _, entry := range components {
		component, ok := entry.(map[string]any)
		if !ok || component["name"] != FrontendComponent {
			continue
		}
		labels, _, err := unstructured.NestedStringMap(component, "podTemplate", "metadata", "labels")
		if err != nil || labels[deploymentIDLabel] != in.Deployment.DeploymentID {
			return fmt.Errorf(
				"deployment %s restates the %s component in its Dynamo overlay without "+
					"%s=%s on its pod template — Dynamo copies those labels onto the Service it "+
					"derives, and that label is how a request already resolved to this deployment "+
					"finds this frontend, so the deployment would come up healthy and be reachable "+
					"by nothing; carry the labels the render puts on the component",
				in.Deployment.DeploymentID, FrontendComponent, deploymentIDLabel,
				in.Deployment.DeploymentID,
			)
		}
		return nil
	}
	return fmt.Errorf(
		"deployment %s writes spec.components in its Dynamo overlay, which replaces the rendered "+
			"list wholesale and takes the %s component with it — the deployment would come up "+
			"healthy and be reachable by nothing; restate the %s component in the overlay, or "+
			"express the change without rewriting spec.components",
		in.Deployment.DeploymentID, FrontendComponent, FrontendComponent,
	)
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

// engineModule maps a declared engine onto the Dynamo entrypoint that runs it.
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

// engineArgs builds the engine command line from the deployment's serving block.
//
// The serving fields are rendered first and engine_args second, so an explicit argument wins over
// the derived one. That direction is deliberate: engine_args is the escape hatch for a flag this
// operator has never heard of, and an escape hatch that loses to a default is not one.
func engineArgs(in Inputs) ([]string, error) {
	model, err := modelRef(in.Artifact)
	if err != nil {
		return nil, err
	}

	derived := map[string]string{
		"model":             model,
		"served-model-name": servedModelName(in),
		"max-model-len":     strconv.Itoa(in.Deployment.Serving.ContextLength),
	}
	if rev := hfRevision(in.Artifact); rev != "" {
		derived["revision"] = rev
	}
	if tp := in.Deployment.Serving.Parallelism.Tensor; tp > 1 {
		derived["tensor-parallel-size"] = strconv.Itoa(tp)
	}
	if pp := in.Deployment.Serving.Parallelism.Pipeline; pp > 1 {
		derived["pipeline-parallel-size"] = strconv.Itoa(pp)
	}
	if f := in.Deployment.Serving.KVCacheFraction; f != nil {
		derived["gpu-memory-utilization"] = strconv.FormatFloat(*f, 'f', -1, 64)
	}
	if v := in.Deployment.Serving.Quantization; v != nil && *v != "" {
		derived["quantization"] = *v
	}
	if v := in.Deployment.Serving.Dtype; v != nil && *v != "" {
		derived["dtype"] = *v
	}
	if v := in.Deployment.Serving.KVCacheDtype; v != nil && *v != "" {
		derived["kv-cache-dtype"] = *v
	}
	if v := in.Deployment.Serving.MaxBatchTokens; v != nil {
		derived["max-num-batched-tokens"] = strconv.Itoa(*v)
	}
	for key, value := range in.Deployment.Serving.EngineArgs {
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
	if !in.Deployment.Serving.PrefixCacheEnabled {
		args = append(args, "--no-enable-prefix-caching")
	}
	return args, nil
}

// servedModelName is the model id an API client sends, as the plane resolved it.
//
// Nothing is derived here and there is nothing left to choose between: the plane collapses the
// workload's own declaration and the artifact's slug before it sends, so the field is never empty
// on the wire and this renders the string it is told. `engine_args["served-model-name"]` still
// beats it, because engine_args is laid over the derived flags.
func servedModelName(in Inputs) string {
	return strings.TrimSpace(in.Deployment.ServedModelName)
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

// applyPodOverlay merges the serving block's pod configuration over the rendered spec.
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

// applyDynamoOverlay merges the serving block's configuration over the rendered graph deployment.
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
