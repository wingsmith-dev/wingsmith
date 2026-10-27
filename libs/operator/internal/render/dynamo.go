// Package render turns declared domain objects into the Kubernetes objects that serve them. It
// is the only operator package that knows Dynamo's and vLLM's shapes. Objects are unstructured so
// the build does not track NVIDIA's Go types, and they are applied server-side.
package render

import (
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"sort"
	"strconv"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/apimachinery/pkg/util/strategicpatch"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// HFHome is the shared model cache mount and HF_HOME in every pod that uses it. One constant
// because the prefetch job and the worker must agree on it exactly.
const HFHome = "/models/hf"

// LocalModels is where a node's own copy of the weights is mounted inside the worker. A second
// root, not a mount over /models, so the init container can read one and write the other.
const LocalModels = "/models-local"

// localModelPath mirrors a path under /models onto the node-local root, keeping the layout so a
// snapshot's sibling files stay where huggingface_hub and the engines expect them.
func localModelPath(shared string) (string, bool) {
	rest, ok := strings.CutPrefix(shared, "/models/")
	if !ok {
		return "", false
	}
	return LocalModels + "/" + rest, true
}

// GVK is the Dynamo graph deployment this operator renders into.
var GVK = schema.GroupVersionKind{Group: "nvidia.com", Version: "v1beta1", Kind: "DynamoGraphDeployment"}

// Options are the per-cluster conventions the render needs and the plane does not declare.
type Options struct {
	Namespace      string
	PoolLabelKey   string
	GPUPoolValue   string
	CachePoolValue string
	CacheTaintKey  string
	// CacheTaintValue is the value the cache pool's taint carries. Empty tolerates any value.
	CacheTaintValue     string
	ModelCachePVC       string
	LocalModelCacheDir  string
	PrefetchCPU         string
	PrefetchMemory      string
	PrefetchMemoryLimit string

	// MultinodeEnabled says whether this cluster can run a replica that spans machines, which
	// needs Grove or LeaderWorkerSet. Without them Dynamo renders one pod holding a fraction of
	// the devices, which hangs in collective initialisation, so the render refuses instead.
	MultinodeEnabled bool

	// GPUTolerationKey and GPUTolerationValue are the taint GPU workers tolerate. An empty key
	// means GPUResource; an empty value tolerates any value of the key.
	GPUTolerationKey   string
	GPUTolerationValue string

	// Fabric names the RDMA fabric workers move KV over: "" for none, FabricEFA for AWS Elastic
	// Fabric Adapter, or FabricRDMA for any other RDMA device plugin. A per-cluster fact: the
	// resource exists only where the device plugin is installed, and asking for a resource no
	// node advertises leaves the pod Pending.
	Fabric string

	// FabricDevices is how many fabric interfaces a worker pod requests. Zero requests none.
	FabricDevices int

	// FabricResource is the extended-resource name for those interfaces. Empty is the AWS EFA
	// plugin's name under FabricEFA, and required under FabricRDMA.
	FabricResource string

	// FabricPrivileged runs fabric workers privileged with host IPC, which NVIDIA's EFA guide
	// requires for registering GPU memory with the NIC. Only honoured when a fabric is set.
	FabricPrivileged bool

	// NIXLBackends names the NIXL plugins the KV connector should register, most specific first.
	// Empty leaves vLLM's default of ["UCX"]. See nixlBackendsFragment for why EC2 needs
	// ["LIBFABRIC"] and what it costs to leave it alone.
	NIXLBackends []string

	// EnginePullSecrets are Secrets in the workload namespace the kubelet presents when pulling
	// the serving block's EngineRuntimeImage. Frontends receive them too because, when neither a
	// deployment nor the cluster names a frontend image, they run that same engine image.
	EnginePullSecrets []string

	// FrontendEnabled renders a frontend beside the worker, one per model. With it false the
	// rendered object is exactly what a cluster-wide frontend arrangement expects.
	FrontendEnabled bool

	// FrontendImage is this cluster's default frontend runtime, used when the deployment names
	// none. Empty falls back to the serving block's engine image.
	FrontendImage string

	// FrontendReplicas is how many frontend pods one model gets. Two by default, so a rollout is
	// not an outage.
	FrontendReplicas int

	// FrontendPullSecrets are Secrets in the workload namespace the kubelet presents when
	// pulling FrontendImage, for a frontend image in a private registry.
	FrontendPullSecrets []string

	// FrontendCPU, FrontendMemory and FrontendMemoryLimit are what one frontend pod asks for.
	// Empty takes the render's defaults. No CPU limit is set: throttling the frontend is latency
	// on the data path.
	FrontendCPU         string
	FrontendMemory      string
	FrontendMemoryLimit string

	// AnthropicAPIEnabled serves `/v1/messages` beside the OpenAI API. AnthropicStripPreamble
	// drops a client-injected, per-request system-prompt prefix so the prefix cache can hit. Both
	// are environment because Dynamo owns the frontend's command line.
	AnthropicAPIEnabled    bool
	AnthropicStripPreamble bool

	// ImagePrefixes are the registries or repositories the images this operator renders must
	// come from: engines, frontends and prefetch jobs, including any image an overlay names. Empty
	// allows every image. The plane chooses those images, so this is the cluster's bound on what
	// it can be told to run.
	ImagePrefixes []string

	// FrontendGlobalNamespace sets globalDynamoNamespace on every component of the graph, the
	// workers as well as the frontend: a frontend in a narrower namespace than its workers would
	// discover none of them. Nil means true, the shared Dynamo namespace. Only read when
	// FrontendEnabled; the cluster-wide frontend always needs the workers in the global namespace.
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

// The annotations recording which inputs a rendered graph was built from. The reconciler
// (`renderIsCurrent`) compares these inputs, not the rendered output, so a renderer change does
// not touch every running workload. Generation and serving digest are the plane's; the layout is computed
// on this cluster and invisible to the plane, hence a third.
const (
	GenerationAnnotation    = "hangar.dev/generation"
	ServingDigestAnnotation = "hangar.dev/serving-digest"
	LayoutAnnotation        = "hangar.dev/layout"
)

// The rollout strategy Dynamo reads off the graph's `spec.annotations`. Dynamo's 25%/25% rounds
// to surge 1 at small counts, which deadlocks a GPU pool with no spare cards; surge 0 /
// unavailable 1 rolls in place.
const (
	MaxSurgeAnnotation       = "nvidia.com/deployment-rolling-update-max-surge"
	MaxUnavailableAnnotation = "nvidia.com/deployment-rolling-update-max-unavailable"

	maxSurge       = "0"
	maxUnavailable = "1"
)

// rolloutStrategy is the graph-level annotation block every render carries.
func rolloutStrategy() map[string]any {
	return map[string]any{
		MaxSurgeAnnotation:       maxSurge,
		MaxUnavailableAnnotation: maxUnavailable,
	}
}

// Layout is how one replica's devices are spread over machines, as the capacity check concluded.
// Passed in so admission and render are one calculation; the zero value is one node.
type Layout struct {
	// DevicesPerNode is what one pod asks for.
	DevicesPerNode int
	// NodesPerReplica is how many pods one replica is.
	NodesPerReplica int
	// Domain is the fabric every pod of the replica must land in, empty when unpinned.
	Domain string
}

// Fingerprint is the layout as one comparable string: nodes, devices per node, and the fabric.
//
// Spelled rather than hashed because it is stamped on the object as an annotation and a person
// reading `2x8/nvlink-domain/ab12` learns something a sha256 would not have told them.
func (l Layout) Fingerprint() string {
	return fmt.Sprintf("%dx%d/%s", l.NodesPerReplica, l.DevicesPerNode, l.Domain)
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

// WorkerComponent is the Dynamo component name for the engine: one aggregated worker unless the
// deployment declares a disaggregated topology.
const WorkerComponent = "VllmDecodeWorker"

// PrefillComponent is the prefill engine of a disaggregated deployment; WorkerComponent stays the
// decode one. Spelling is Dynamo's (see FrontendComponent), and `TestComponentBudget` holds its
// length to maxWorkloadName.
const PrefillComponent = "VllmPrefillWorker"

// FrontendComponent is the Dynamo component name for the frontend. Dynamo puts it in the
// `nvidia.com/dynamo-component` label that the agent's resolver and the benchmark executor match
// on, so renaming it breaks both silently.
const FrontendComponent = "Frontend"

// Disaggregated says whether a deployment's roles are separate processes. Topology and roles must
// agree; an old plane sends neither and renders as before.
func Disaggregated(dep wire.DeploymentDesired) bool {
	return dep.Serving.Topology == "disaggregated" && len(dep.Serving.Roles) > 0
}

// workerRole is one worker component to render. The aggregated case is a single descriptor with
// an empty Role, and every disaggregated addition is guarded on Role so its output is unchanged.
type workerRole struct {
	// Component is the Dynamo component name, which is also a label value two lookups match on.
	Component string
	// Type is the `type` Dynamo puts on the component: `decode` for the aggregated worker as
	// well, because an aggregated worker decodes — it just prefills too.
	Type string
	// Role is `prefill` or `decode` on a disaggregated deployment, and empty on an aggregated
	// one. Empty is the discriminator for every conditional in this file.
	Role string
	// Replicas is the absolute worker count, taken from the wire and never computed here. See
	// `Replicas` on why this operator does no arithmetic on a replica count.
	Replicas int
	// Spec is the per-role configuration, nil when aggregated.
	Spec *wire.RoleSpec
}

// WorkerRoles is the worker components this deployment renders, in the plane's order: Dynamo
// hashes the components together, so a re-order would rename and reload them.
func WorkerRoles(dep wire.DeploymentDesired) ([]workerRole, error) {
	if IsOmni(dep) {
		if Disaggregated(dep) {
			return nil, fmt.Errorf("the omni engine has no prefill/decode split to disaggregate")
		}
		return []workerRole{{
			Component: OmniComponent,
			Type:      omniType,
			Replicas:  Replicas(dep),
		}}, nil
	}
	if !Disaggregated(dep) {
		return []workerRole{{
			Component: WorkerComponent,
			Type:      "decode",
			Replicas:  Replicas(dep),
		}}, nil
	}
	roles := make([]workerRole, 0, len(dep.Serving.Roles))
	for i := range dep.Serving.Roles {
		spec := &dep.Serving.Roles[i]
		component, kind, err := roleComponent(spec.Role)
		if err != nil {
			return nil, err
		}
		count, ok := dep.RoleReplicas[spec.Role]
		if !ok {
			// The plane multiplies groups by weights and sends the product. A role with a
			// configuration and no count is a frame that lost half of itself in transit, and
			// rendering it at some default would serve a ratio nobody declared.
			return nil, fmt.Errorf("role %q has no replica count on the wire", spec.Role)
		}
		if count < 0 {
			count = 0
		}
		roles = append(roles, workerRole{Component: component, Type: kind, Role: spec.Role, Replicas: count, Spec: spec})
	}
	return roles, nil
}

// roleComponent maps a declared role onto its Dynamo component. An unknown role is an error,
// since the plane already refuses it and a guess would hide the disagreement.
func roleComponent(role string) (component, kind string, err error) {
	switch role {
	case "prefill":
		return PrefillComponent, "prefill", nil
	case "decode":
		return WorkerComponent, "decode", nil
	default:
		return "", "", fmt.Errorf("unknown serving role %q", role)
	}
}

// DynamoGraphDeployment renders one deployment into the object that serves it: the model's
// workers and, where the cluster opted in, its own frontend.
func DynamoGraphDeployment(in Inputs, opts Options) (*unstructured.Unstructured, error) {
	if err := checkOverlay(in.Deployment.Serving.Overlay); err != nil {
		return nil, fmt.Errorf("deployment %s: %w", in.Deployment.DeploymentID, err)
	}
	roles, err := WorkerRoles(in.Deployment)
	if err != nil {
		return nil, err
	}
	components := make([]any, 0, len(roles)+1)
	for _, role := range roles {
		pod, err := podSpec(in, opts, role)
		if err != nil {
			return nil, err
		}
		podMap, err := runtime.DefaultUnstructuredConverter.ToUnstructured(pod)
		if err != nil {
			return nil, fmt.Errorf("convert pod spec: %w", err)
		}
		component := map[string]any{
			"name":                  role.Component,
			"type":                  role.Type,
			"replicas":              int64(role.Replicas),
			"globalDynamoNamespace": globalNamespace(opts),
			"podTemplate": map[string]any{
				"metadata": map[string]any{"labels": ownershipLabels(in, role)},
				"spec":     podMap,
			},
		}
		// A replica that spans machines is one leader-worker group, not several replicas. Dynamo
		// renders it from the same pod template, which is why the device request above is per node
		// and not per replica.
		if n := in.Layout.nodes(); n > 1 {
			component["multinode"] = map[string]any{"nodeCount": int64(n)}
		}
		components = append(components, component)
	}
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
			"labels":    ownershipLabels(in, workerRole{}),
			"annotations": map[string]any{
				GenerationAnnotation:    strconv.FormatInt(in.Deployment.Generation, 10),
				ServingDigestAnnotation: in.Deployment.Serving.Digest,
				LayoutAnnotation:        in.Layout.Fingerprint(),
			},
		},
		"spec": map[string]any{
			// Set on the first render rather than patched on later: Dynamo copies
			// `spec.annotations` into every worker pod template and hashes it, so arriving late
			// costs a full weight reload.
			"annotations": rolloutStrategy(),
			"components":  components,
		},
	}}

	if err := applyDynamoOverlay(obj, in.Deployment.Serving.Overlay.Dynamo); err != nil {
		return nil, err
	}
	obj.Object = withoutContentLoggingEnv(obj.Object).(map[string]any)
	if err := checkImages(obj.Object, opts); err != nil {
		return nil, fmt.Errorf("deployment %s: %w", in.Deployment.DeploymentID, err)
	}
	return obj, nil
}

// Name renders a prefixed entity id as a Kubernetes object name.
func Name(id string) string {
	return strings.ToLower(strings.ReplaceAll(id, "_", "-"))
}

// maxWorkloadName bounds the rendered name so the DynamoComponentDeployment Dynamo derives from it,
// whose name is also a 63-byte label value, is not refused. A chosen number: deriving it from the
// components would rename every deployment when one is added. `TestComponentBudget` holds the
// components to it.
const maxWorkloadName = 32

// componentNameBudget is what a component name may spend: what is left of the 63 bytes after the
// workload name, two dashes and the eight-character spec hash. `VllmPrefillWorker` (17) is the
// longest this renderer emits; a component past the budget fails `TestComponentBudget`.
const componentNameBudget = maxObjectNameBytes - maxWorkloadName - len("-") - len("-") - len(specHashPlaceholder)

// maxObjectNameBytes is the label-value limit the derived name is actually held to.
const maxObjectNameBytes = 63

// specHashPlaceholder stands in for the hash Dynamo derives for worker components; only its
// length is used. Dynamo hashes all workers together, which puts both disaggregated roles in one
// runtime namespace, so they must stay in one graph.
const specHashPlaceholder = "00000000"

// WorkloadName is the object name one deployment renders under: its name, unique per cluster and
// readable in `kubectl get pods`, or its id when the name renders to nothing.
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

// ownershipLabels mark what this operator renders as ours. They go on the pod template too, because
// discovery reads pod labels and Dynamo does not copy the graph's labels onto its pods. The role
// label is added only for disaggregated deployments, so an aggregated template, which Dynamo
// hashes into the object name, stays unchanged.
func ownershipLabels(in Inputs, role workerRole) map[string]any {
	labels := map[string]any{
		deploymentIDLabel:       in.Deployment.DeploymentID,
		"hangar.dev/project":    in.Deployment.ProjectID,
		"hangar.dev/managed-by": "hangar-operator",
	}
	if role.Role != "" {
		labels[RoleLabel] = role.Role
	}
	return labels
}

// roleLabel says which role of a disaggregated deployment a pod is. Absent on an aggregated one,
// where there is one worker and nothing to distinguish.
const RoleLabel = "hangar.dev/role"

// Replicas is this cluster's share of the workload, as the plane divided it; render exactly that.
// MinReplicas, because nothing here autoscales. Exported so the reconciler judges completeness by
// the same number it renders.
func Replicas(dep wire.DeploymentDesired) int {
	if dep.MinReplicas < 0 {
		return 0
	}
	return dep.MinReplicas
}

// localizeScript mirrors one model directory onto the node's disk before the engine starts.
// Afterwards $DST is either the mirror or a symlink to the shared copy, so --model never changes
// and a failure is only a slow start. $DST/.hangar-complete is written last, so an interrupted
// copy is redone, never served.
const localizeScript = `set -eu
SRC="$1"
DST="$2"

# The marker alone is not enough to trust a mirror: an earlier build wrote it over a directory
# of dangling symlinks. A correct mirror holds files, so a top-level symlink means the copy came
# from the version that did not dereference, and the mirror is redone.
if [ -e "$DST/.hangar-complete" ] && [ -z "$(find "$DST" -maxdepth 1 -type l -print 2>/dev/null | head -n1)" ]; then
  echo "weights already on this node: $DST"
  exit 0
fi

fallback() {
  echo "localizing failed ($1); reading weights from the shared cache instead"
  rm -rf "$DST" "$DST.partial"
  mkdir -p "$(dirname "$DST")"
  ln -sfn "$SRC" "$DST"
  exit 0
}

# -L on both, because a huggingface_hub snapshot directory is symlinks into ../../blobs and
# nothing in it is a file. Without dereferencing, the copy takes 492 KB of links pointing
# outside the mirror, du agrees the model is 492 KB, so the space check passes, the copy
# reports success, and the marker is written over a directory holding no weights at all. The
# engine then reads a path that exists and is empty -- which the fallback cannot catch,
# because the fallback tests for the directory rather than for its contents.
#
# Checked before copying rather than discovered during it: a node whose disk cannot hold the model
# would otherwise spend the whole download finding that out, and the fallback is free.
need=$(du -skL "$SRC" | cut -f1)
mkdir -p "$(dirname "$DST")"
free=$(df -Pk "$(dirname "$DST")" | awk "NR==2 {print \$4}")
if [ "$free" -lt "$need" ]; then
  fallback "need ${need}K, ${free}K free"
fi

echo "copying ${need}K from $SRC to $DST"
rm -rf "$DST" "$DST.partial"
mkdir -p "$DST.partial"
cp -aL "$SRC/." "$DST.partial/" || fallback "copy error"
touch "$DST.partial/.hangar-complete"
mv "$DST.partial" "$DST"
echo "weights localized: $DST"
`

// localizeContainers is the init container that fills the node-local mirror, or nothing. It runs
// the engine image and requests no GPU, since it is only a file copy.
func localizeContainers(in Inputs, opts Options, image string) []corev1.Container {
	if opts.LocalModelCacheDir == "" {
		return nil
	}
	shared, isPath, err := modelRef(in.Artifact)
	if err != nil || !isPath {
		return nil
	}
	local, ok := localModelPath(shared)
	if !ok {
		return nil
	}
	return []corev1.Container{{
		Name:                     "localize-weights",
		Image:                    image,
		Command:                  []string{"sh", "-c", localizeScript, "localize", shared, local},
		TerminationMessagePolicy: corev1.TerminationMessageFallbackToLogsOnError,
		VolumeMounts: append(localModelMount(opts, false), corev1.VolumeMount{
			Name: "model-cache", MountPath: "/models", ReadOnly: true,
		}),
	}}
}

// frontendLocalizeScript gives the frontend the metadata at the path the workers advertise, without
// the weights, into an emptyDir. It writes no `.hangar-complete`, so it is never mistaken for a
// mirror.
const frontendLocalizeScript = `set -eu
SRC="$1"
DST="$2"

mkdir -p "$DST"
# Everything a tokenizer or a config might be, and nothing that could be a tensor. Listing what to
# take rather than what to skip: a new weight file extension would otherwise be copied by default,
# and a model's weights in an emptyDir fill the node's disk rather than failing honestly.
# -type l as well as -type f, and cp -L: in a huggingface_hub cache every entry in a snapshot
# directory is a symlink into ../../blobs/, so -type f alone matches nothing and stages an empty
# directory -- which fails exactly like having no directory at all, only later and more quietly.
find "$SRC" -maxdepth 1 \( -type f -o -type l \) \
  \( -name '*.json' -o -name '*.txt' -o -name '*.py' -o -name '*.model' -o -name '*.jinja' \) \
  -exec cp -L {} "$DST/" \;
staged=$(ls -1 "$DST" | wc -l)
if [ "$staged" -eq 0 ]; then
  # Loudly, because the quiet version of this failure is worse: the frontend
  # comes up, passes every probe, and serves an empty model list. A pod that will not start is a
  # far cheaper way to learn that the source layout is not what this script assumed.
  echo "staged no metadata from $SRC; refusing to start a frontend that would serve nothing" >&2
  ls -la "$SRC" | head -20 >&2
  exit 1
fi
echo "model metadata staged at $DST ($staged files):"
ls "$DST"
`

// frontendLocalizeContainers gives the frontend the path the workers advertise. Without it the
// frontend treats `/models-local/...` as a Hub repo id and serves an empty model list.
func frontendLocalizeContainers(in Inputs, opts Options, image string) []corev1.Container {
	if opts.LocalModelCacheDir == "" {
		return nil
	}
	shared, isPath, err := modelRef(in.Artifact)
	if err != nil || !isPath {
		return nil
	}
	local, ok := localModelPath(shared)
	if !ok {
		return nil
	}
	return []corev1.Container{{
		Name:                     "stage-model-metadata",
		Image:                    image,
		SecurityContext:          frontendContainerSecurityContext(),
		Command:                  []string{"sh", "-c", frontendLocalizeScript, "stage", shared, local},
		TerminationMessagePolicy: corev1.TerminationMessageFallbackToLogsOnError,
		VolumeMounts: []corev1.VolumeMount{
			{Name: "local-model-cache", MountPath: LocalModels},
			{Name: "model-cache", MountPath: "/models", ReadOnly: true},
		},
	}}
}

// frontendLocalModelVolume is scratch, not the host disk. See frontendLocalizeScript.
func frontendLocalModelVolume(opts Options) []corev1.Volume {
	if opts.LocalModelCacheDir == "" {
		return nil
	}
	return []corev1.Volume{{
		Name:         "local-model-cache",
		VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
	}}
}

// localModelVolume is the node's own disk, or nothing. A hostPath so the mirror outlives the pod;
// DirectoryOrCreate so a node with no mirror yet still schedules.
func localModelVolume(opts Options) []corev1.Volume {
	if opts.LocalModelCacheDir == "" {
		return nil
	}
	kind := corev1.HostPathDirectoryOrCreate
	return []corev1.Volume{{
		Name: "local-model-cache",
		VolumeSource: corev1.VolumeSource{
			HostPath: &corev1.HostPathVolumeSource{Path: opts.LocalModelCacheDir, Type: &kind},
		},
	}}
}

// localModelMount mounts that disk: writable for the init container that fills it, read-only for
// the engine, which has no reason to change a mirror other workers on the node also load from.
func localModelMount(opts Options, readOnly bool) []corev1.VolumeMount {
	if opts.LocalModelCacheDir == "" {
		return nil
	}
	return []corev1.VolumeMount{{Name: "local-model-cache", MountPath: LocalModels, ReadOnly: readOnly}}
}

// workerProbes replace Dynamo's defaults (liveness failureThreshold 1, 4s timeout), under which a
// saturated worker's 503 gets it killed. Readiness stays quick; liveness allows three minutes; the
// startup probe is Dynamo's.
func workerProbes() (liveness, readiness *corev1.Probe) {
	probe := func(path string, period, timeout, threshold int32) *corev1.Probe {
		return &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				HTTPGet: &corev1.HTTPGetAction{
					Path:   path,
					Port:   intstr.FromString("system"),
					Scheme: corev1.URISchemeHTTP,
				},
			},
			PeriodSeconds:    period,
			TimeoutSeconds:   timeout,
			FailureThreshold: threshold,
			SuccessThreshold: 1,
		}
	}
	return probe("/live", 30, 15, 6), probe("/health", 10, 10, 3)
}

// GPUResource is the extended resource a worker asks for its accelerators by. Hangar targets
// NVIDIA GPUs through the NVIDIA device plugin, and discovery reads the same name.
const GPUResource = corev1.ResourceName("nvidia.com/gpu")

// The fabrics a cluster can declare.
const (
	FabricEFA  = "efa"
	FabricRDMA = "rdma"
)

// efaResource is the extended resource the AWS EFA device plugin advertises.
const efaResource = "vpc.amazonaws.com/efa"

// efaHugePages is what NVIDIA's EFA-on-AWS guide requests alongside FI_EFA_USE_HUGE_PAGE=1.
const efaHugePages = "5120Mi"

// fabricOn says whether worker pods request fabric interfaces at all.
func fabricOn(opts Options) bool {
	return opts.Fabric != "" && opts.FabricDevices > 0
}

// fabricResource is the extended resource the declared fabric is requested by.
func fabricResource(opts Options) (corev1.ResourceName, error) {
	if name := strings.TrimSpace(opts.FabricResource); name != "" {
		return corev1.ResourceName(name), nil
	}
	if opts.Fabric == FabricEFA {
		return efaResource, nil
	}
	return "", fmt.Errorf("fabric %q needs a resource name; set HANGAR_FABRIC_RESOURCE", opts.Fabric)
}

// fabricPrivileged says whether a worker runs privileged with host IPC for its fabric.
func fabricPrivileged(opts Options) bool {
	return fabricOn(opts) && opts.FabricPrivileged
}

// fabricSecurityContext lets NIXL register GPU memory with the NIC: IPC_LOCK, plus privileged where
// the cluster's fabric needs it (EFA does). Opt-in per cluster; drop the privileged half once NIXL
// registers through DMA-BUF.
func fabricSecurityContext(opts Options) *corev1.SecurityContext {
	if !fabricOn(opts) {
		return nil
	}
	sc := &corev1.SecurityContext{Capabilities: &corev1.Capabilities{Add: []corev1.Capability{"IPC_LOCK"}}}
	if fabricPrivileged(opts) {
		sc.Privileged = ptr(true)
	}
	return sc
}

// hugePagesAllowed says whether Kubernetes will accept a huge-page request on this container.
// It will not unless cpu or memory is also requested, and the rejection happens where almost
// nothing reads it.
func hugePagesAllowed(requests corev1.ResourceList) bool {
	_, cpu := requests[corev1.ResourceCPU]
	_, mem := requests[corev1.ResourceMemory]
	return cpu || mem
}

// fabricEnvFor is libfabric's EFA configuration, on an EFA fabric only, with huge pages turned
// off when the pod could not reserve any.
func fabricEnvFor(opts Options, hugePages bool) []corev1.EnvVar {
	if !fabricOn(opts) || opts.Fabric != FabricEFA {
		return nil
	}
	env := fabricEnv()
	if !hugePages {
		for i := range env {
			if env[i].Name == "FI_EFA_USE_HUGE_PAGE" {
				env[i].Value = "0"
			}
		}
	}
	return env
}

// fabricEnv pins libfabric to the EFA provider (shm and tcp would also "work" while moving
// nothing over the fabric) and applies the tuning from NVIDIA's EFA-on-AWS guide.
func fabricEnv() []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: "FI_PROVIDER", Value: "efa"},
		{Name: "FI_EFA_USE_DEVICE_RDMA", Value: "1"},
		{Name: "FI_EFA_ENABLE_SHM_TRANSFER", Value: "0"},
		{Name: "FI_EFA_ENABLE_SHM", Value: "0"},
		{Name: "FI_EFA_FORK_SAFE", Value: "0"},
		{Name: "FI_EFA_USE_HUGE_PAGE", Value: "1"},
		{Name: "FI_EFA_MR_MAX_CACHED_COUNT", Value: "524288"},
		{Name: "FI_EFA_MR_MAX_CACHED_SIZE", Value: "0"},
	}
}

func podSpec(in Inputs, opts Options, role workerRole) (*corev1.PodSpec, error) {
	devices := in.Layout.devices(in.Deployment)

	requests := corev1.ResourceList{GPUResource: *resource.NewQuantity(int64(devices), resource.DecimalSI)}
	limits := corev1.ResourceList{GPUResource: *resource.NewQuantity(int64(devices), resource.DecimalSI)}
	if n := in.Deployment.Serving.Resources.CPUCores; n != nil {
		requests[corev1.ResourceCPU] = *resource.NewQuantity(int64(*n), resource.DecimalSI)
	}
	if n := in.Deployment.Serving.Resources.MemoryMiB; n != nil {
		requests[corev1.ResourceMemory] = *resource.NewQuantity(*n*1024*1024, resource.BinarySI)
	}
	if n := in.Deployment.Serving.Resources.DiskGiB; n != nil {
		requests[corev1.ResourceEphemeralStorage] = *resource.NewQuantity(int64(*n)*1024*1024*1024, resource.BinarySI)
	}

	// The fabric is an extended resource, so request equals limit. Every worker asks: the
	// resource is per node and one worker owns its node, and a worker that needed it and did not
	// ask silently runs KV transfers over TCP.
	if fabricOn(opts) {
		name, err := fabricResource(opts)
		if err != nil {
			return nil, err
		}
		count := *resource.NewQuantity(int64(opts.FabricDevices), resource.DecimalSI)
		requests[name] = count
		limits[name] = count

		// EFA's bounce buffers come from huge pages the host reserves at boot, claimed here by
		// name. Kubernetes rejects a huge-page request without cpu or memory, so it is made only
		// when one is declared, and fabricEnvFor turns FI_EFA_USE_HUGE_PAGE off to match.
		if opts.Fabric == FabricEFA && hugePagesAllowed(requests) {
			huge := resource.MustParse(efaHugePages)
			requests[corev1.ResourceHugePagesPrefix+"2Mi"] = huge
			limits[corev1.ResourceHugePagesPrefix+"2Mi"] = huge
		}
	}

	// A host KV offload pool is large and unbounded by the GPU, so it gets a memory limit: the pool
	// plus the declared request. Without a declared request there is no limit, since any ceiling
	// would be a guess. Only rendered when the tier is on.
	if gib, on := offloadTier(in); on {
		if declared, ok := requests[corev1.ResourceMemory]; ok {
			ceiling := declared.DeepCopy()
			ceiling.Add(*resource.NewQuantity(int64(gib)*1024*1024*1024, resource.BinarySI))
			limits[corev1.ResourceMemory] = ceiling
		}
	}

	image := ""
	if declared := in.Deployment.Serving.RuntimeImage(); declared != nil {
		image = strings.TrimSpace(*declared)
	}
	if image == "" {
		return nil, fmt.Errorf("deployment %s names no engine runtime image", in.Deployment.DeploymentID)
	}

	module, err := engineModule(in.Deployment.Serving.Engine)
	if err != nil {
		return nil, err
	}
	var (
		args        []string
		stageConfig string
	)
	if module == "vllm.omni" {
		if stageConfig, err = omniStageConfig(in); err != nil {
			return nil, err
		}
		args, err = omniArgs(in, opts, stageConfig != "")
	} else {
		if m := in.Deployment.Serving.OutputModality; m != "" && m != "text" {
			return nil, fmt.Errorf("the %s engine emits text and cannot serve output modality %q; "+
				"image and video are the omni engine's", in.Deployment.Serving.Engine, m)
		}
		args, err = engineArgs(in, opts, role)
	}
	if err != nil {
		return nil, err
	}

	// Tensor-parallel workers exchange activations over shared memory. The container default of
	// 64MB is far too small, and the failure is a NCCL or bus error at startup that never names
	// /dev/shm as the cause — so it is sized here rather than left to be rediscovered.
	shm := resource.MustParse("16Gi")
	liveness, readiness := workerProbes()

	spec := &corev1.PodSpec{
		ImagePullSecrets: imagePullSecrets(opts.EnginePullSecrets),
		NodeSelector:     podNodeSelector(in, opts),
		// Paired with the privileged context; see fabricSecurityContext.
		HostIPC:     fabricPrivileged(opts),
		Tolerations: []corev1.Toleration{taintToleration(gpuTolerationKey(opts), opts.GPUTolerationValue)},
		Volumes: append(localModelVolume(opts), []corev1.Volume{
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
		}...),
		InitContainers: localizeContainers(in, opts, image),
		Containers: []corev1.Container{{
			Name:       "main",
			Image:      image,
			WorkingDir: "/workspace/examples/backends/vllm",
			Command:    []string{"python3", "-m", "dynamo." + module},
			Args:       args,
			// Have the kubelet copy the tail of a failing container's output into
			// lastState.terminated.message, which the diagnosis reads. Nothing writes
			// /dev/termination-log.
			TerminationMessagePolicy: corev1.TerminationMessageFallbackToLogsOnError,
			SecurityContext:          fabricSecurityContext(opts),
			LivenessProbe:            liveness,
			ReadinessProbe:           readiness,
			Env: append(fabricEnvFor(opts, hugePagesAllowed(requests)), []corev1.EnvVar{
				{Name: "HF_HOME", Value: HFHome},
				// KV-aware routing decides which worker already holds a prefix by hashing
				// block contents. Python salts hashes per process by default, so without a
				// fixed seed the frontend and the workers hash identical blocks differently
				// and every cache hit is missed — silently, as a performance loss.
				{Name: "PYTHONHASHSEED", Value: "0"},
			}...),
			EnvFrom: []corev1.EnvFromSource{{
				SecretRef: &corev1.SecretEnvSource{
					LocalObjectReference: corev1.LocalObjectReference{Name: "hf-token-secret"},
					Optional:             ptr(true),
				},
			}},
			VolumeMounts: append(localModelMount(opts, true), []corev1.VolumeMount{
				{Name: "model-cache", MountPath: "/models"},
				{Name: "dshm", MountPath: "/dev/shm"},
			}...),
			Resources: corev1.ResourceRequirements{Requests: requests, Limits: limits},
		}},
	}

	if stageConfig != "" {
		spec.Volumes = append(spec.Volumes, corev1.Volume{
			Name:         omniStageVolume,
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		})
		spec.InitContainers = append(spec.InitContainers, omniStageContainer(image, stageConfig))
		spec.Containers[0].VolumeMounts = append(spec.Containers[0].VolumeMounts, corev1.VolumeMount{
			Name: omniStageVolume, MountPath: omniStageDir, ReadOnly: true,
		})
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

// frontendComponent renders the frontend this model is served through. Its pod template carries
// ownershipLabels, which Dynamo copies onto the derived Service where the agent finds it.
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

	return map[string]any{
		// `type: frontend` is what earns the component its ports and the command it runs;
		// `name: Frontend` is what the two label-based lookups match on. Different fields
		// saying different things, and both load-bearing.
		"name":                  FrontendComponent,
		"type":                  "frontend",
		"replicas":              int64(replicas),
		"globalDynamoNamespace": globalNamespace(opts),
		"podTemplate": map[string]any{
			"metadata": map[string]any{"labels": ownershipLabels(in, workerRole{})},
			"spec":     podMap,
		},
	}, nil
}

// frontendPodSpec is one frontend pod: no GPU selector or toleration, no command (Dynamo derives it
// from `type: frontend`), and no pod overlay, which is written for the engine. The model cache is
// mounted read-only because the frontend builds its tokenizer from the path the worker advertises.
func frontendPodSpec(in Inputs, opts Options) (*corev1.PodSpec, error) {
	image, err := frontendImage(in, opts)
	if err != nil {
		return nil, err
	}
	router, err := frontendRouterEnv(in.Deployment.Serving.Router, IsOmni(in.Deployment))
	if err != nil {
		return nil, fmt.Errorf("deployment %s: %w", in.Deployment.DeploymentID, err)
	}
	resources, err := frontendResources(opts)
	if err != nil {
		return nil, err
	}

	spec := &corev1.PodSpec{
		// Engine credentials are included because the frontend falls back to EngineRuntimeImage when no
		// frontend-specific image is configured. Duplicates are removed when both settings name
		// the same fleet-wide credential.
		ImagePullSecrets: imagePullSecrets(opts.FrontendPullSecrets, opts.EnginePullSecrets),
		SecurityContext:  frontendPodSecurityContext(),
		Containers: []corev1.Container{{
			Name:            "main",
			Image:           image,
			SecurityContext: frontendContainerSecurityContext(),
			// The same reason as the worker's: a frontend that exits on a bad flag or a
			// missing credential should say so where the plane can read it.
			TerminationMessagePolicy: corev1.TerminationMessageFallbackToLogsOnError,
			Env: append([]corev1.EnvVar{
				{Name: "DYN_ENABLE_ANTHROPIC_API", Value: strconv.FormatBool(opts.AnthropicAPIEnabled)},
				{Name: "DYN_STRIP_ANTHROPIC_PREAMBLE", Value: strconv.FormatBool(opts.AnthropicStripPreamble)},
				// Same fixed seed as the worker: KV-aware routing hashes block contents,
				// and different salts would silently miss every cache hit.
				{Name: "PYTHONHASHSEED", Value: "0"},
			}, router...),
			Resources: resources,
		}},
	}

	// Named by configuration and therefore able to be empty. A volume claiming nothing is
	// rejected by the API server, so a cluster with no cache volume gets a frontend without
	// one rather than a deployment that will not apply.
	if claim := strings.TrimSpace(opts.ModelCachePVC); claim != "" {
		spec.Volumes = []corev1.Volume{{
			Name: "model-cache",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: claim,
					ReadOnly:  true,
				},
			},
		}}
		spec.Containers[0].VolumeMounts = []corev1.VolumeMount{
			{Name: "model-cache", MountPath: "/models", ReadOnly: true},
		}

		// The workers advertise the node-local mirror, so the frontend has to be able to open
		// that same path -- see frontendLocalizeScript. Nested inside the cache-claim branch
		// because the metadata is copied *from* the claim: with no cache volume there is
		// nothing to stage from, and the workers are not localizing either.
		if init := frontendLocalizeContainers(in, opts, image); len(init) > 0 {
			spec.InitContainers = init
			spec.Volumes = append(spec.Volumes, frontendLocalModelVolume(opts)...)
			spec.Containers[0].VolumeMounts = append(spec.Containers[0].VolumeMounts,
				corev1.VolumeMount{Name: "local-model-cache", MountPath: LocalModels, ReadOnly: true})
		}
	}

	return spec, nil
}

// frontendPodSecurityContext gives the frontend the runtime's default seccomp profile. It keeps
// the image's user: the frontend runs the engine image when no frontend image is configured, and
// that image's user owns the cache directories it writes.
func frontendPodSecurityContext() *corev1.PodSecurityContext {
	return &corev1.PodSecurityContext{
		SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
	}
}

// frontendContainerSecurityContext drops every capability and privilege escalation. A frontend
// serves HTTP on an unprivileged port and reads its model metadata; it needs neither.
func frontendContainerSecurityContext() *corev1.SecurityContext {
	return &corev1.SecurityContext{
		AllowPrivilegeEscalation: ptr(false),
		Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
	}
}

// DefaultRouterMode is the frontend's routing mode when a deployment declares none.
const DefaultRouterMode = "kv"

// OmniRouterMode is the default for an omni deployment instead. A diffusion request has no prefix
// to reuse and an omni worker publishes no KV events, and Dynamo 1.5.0's image path through a
// `kv` frontend fails every request with "KV routing should not call generate on PushRouter".
const OmniRouterMode = "round-robin"

// routerModes is every `DYN_ROUTER_MODE` a deployment may declare: Dynamo 1.5.0's `--router-mode`
// choices less `direct`, which routes to a worker id the request has to carry. Nothing in front of a
// Hangar frontend sets one, so a frontend in that mode refuses every request it is sent.
var routerModes = map[string]bool{
	"kv": true, "round-robin": true, "random": true, "power-of-two": true,
	"least-loaded": true, "device-aware-weighted": true,
}

// routerQueuePolicies is Dynamo 1.5.0's `--router-queue-policy` choices.
var routerQueuePolicies = map[string]bool{"fcfs": true, "wspt": true}

// frontendRouterEnv is the frontend's router configuration as environment. `DYN_ROUTER_MODE` is
// always rendered, `kv` by default, since round-robin halves the prefix hit rate. The rest render
// only when declared, in a fixed order, because the pod template is hashed. The worker's half of
// KV mode is `kvEventsConfig`.
func frontendRouterEnv(router *wire.RouterSpec, omni bool) ([]corev1.EnvVar, error) {
	mode := DefaultRouterMode
	if omni {
		mode = OmniRouterMode
	}
	if router != nil && router.Mode != nil {
		mode = *router.Mode
	}
	if !routerModes[mode] {
		return nil, fmt.Errorf("unknown router mode %q", mode)
	}
	env := []corev1.EnvVar{{Name: "DYN_ROUTER_MODE", Value: mode}}
	if router == nil {
		return env, nil
	}
	if v := router.QueueThreshold; v != nil {
		value, err := routerFloat("queue_threshold", *v)
		if err != nil {
			return nil, err
		}
		env = append(env, corev1.EnvVar{Name: "DYN_ROUTER_QUEUE_THRESHOLD", Value: value})
	}
	if p := router.QueuePolicy; p != nil {
		if !routerQueuePolicies[*p] {
			return nil, fmt.Errorf("unknown router queue policy %q", *p)
		}
		env = append(env, corev1.EnvVar{Name: "DYN_ROUTER_QUEUE_POLICY", Value: *p})
	}
	if v := router.KVOverlapScoreCredit; v != nil {
		value, err := routerFloat("kv_overlap_score_credit", *v)
		if err != nil {
			return nil, err
		}
		env = append(env, corev1.EnvVar{Name: "DYN_ROUTER_KV_OVERLAP_SCORE_CREDIT", Value: value})
	}
	return env, nil
}

// routerFloat spells a router number as Python does (`1.0`), because the spelling is part of the
// hashed pod template.
func routerFloat(field string, v float64) (string, error) {
	if math.IsNaN(v) || math.IsInf(v, 0) || v < 0 {
		return "", fmt.Errorf("router %s must be a finite number >= 0, got %v", field, v)
	}
	out := strconv.FormatFloat(v, 'f', -1, 64)
	if !strings.Contains(out, ".") {
		out += ".0"
	}
	return out, nil
}

// imagePullSecrets converts configured names into the pod form, preserving their order while
// removing duplicates. One registry credential commonly serves both the frontend and engine
// images, and presenting the same LocalObjectReference twice says nothing useful to the kubelet.
func imagePullSecrets(groups ...[]string) []corev1.LocalObjectReference {
	seen := make(map[string]struct{})
	var refs []corev1.LocalObjectReference
	for _, names := range groups {
		for _, name := range names {
			if name == "" {
				continue
			}
			if _, exists := seen[name]; exists {
				continue
			}
			seen[name] = struct{}{}
			refs = append(refs, corev1.LocalObjectReference{Name: name})
		}
	}
	return refs
}

// frontendImage resolves which runtime the frontend runs, most specific first: the deployment's
// own frontend image (a model may need a patched frontend beside a stock worker), then the
// cluster default, then the engine image, which usually carries the frontend too.
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
	if declared := serving.RuntimeImage(); declared != nil {
		if image := strings.TrimSpace(*declared); image != "" {
			return image, nil
		}
	}
	return "", fmt.Errorf(
		"deployment %s names no frontend image and no engine runtime image to fall back on",
		in.Deployment.DeploymentID)
}

// frontendResources is what one frontend pod requests: a memory ceiling and no CPU limit, since
// CPU throttling is latency on the data path.
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

// globalNamespace is the globalDynamoNamespace every component of a graph renders with.
//
// Without per-model frontends the setting does not apply: the only frontend is the cluster-wide
// one, which lives in the global namespace, and workers anywhere else would be invisible to it.
func globalNamespace(opts Options) bool {
	if !opts.FrontendEnabled {
		return true
	}
	if opts.FrontendGlobalNamespace != nil {
		return *opts.FrontendGlobalNamespace
	}
	return true
}

// gpuTolerationKey is the taint key GPU workers tolerate.
func gpuTolerationKey(opts Options) string {
	if key := strings.TrimSpace(opts.GPUTolerationKey); key != "" {
		return key
	}
	return string(GPUResource)
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
	if IsOmni(dep) {
		out[archLabel] = "amd64"
	}
	return out
}

// archLabel is the node label for CPU architecture. Omni workers are pinned to amd64 because
// vLLM-Omni has no arm64 build; the capacity check reads the same selector.
const archLabel = "kubernetes.io/arch"

// podNodeSelector is NodeSelector plus the fabric domain the capacity check settled on.
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

// engineModule maps a declared engine onto its Dynamo entrypoint. Unknown engines are an error,
// not a fall-through to vllm.
func engineModule(engine string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(engine)) {
	case "vllm":
		return "vllm", nil
	case "sglang":
		return "sglang", nil
	case "trtllm", "tensorrt-llm", "tensorrt_llm":
		return "trtllm", nil
	case "omni", "vllm-omni", "vllm_omni":
		return "vllm.omni", nil
	default:
		return "", fmt.Errorf("unknown engine %q: this operator renders vllm, sglang, trtllm and omni", engine)
	}
}

// IsOmni says whether a deployment runs the vLLM-Omni diffusion engine.
func IsOmni(dep wire.DeploymentDesired) bool {
	module, err := engineModule(dep.Serving.Engine)
	return err == nil && module == "vllm.omni"
}

// OmniComponent is the Dynamo component for a vLLM-Omni worker, with type `worker`, from which
// Dynamo derives ports, frontend detection and the component-type label.
const (
	OmniComponent = "OmniWorker"
	omniType      = "worker"
)

// Output modalities an omni worker can be told to emit.
const (
	OutputImage = "image"
	OutputVideo = "video"
)

// omniMediaURL is where the omni worker writes output before returning it inline. Pod-local,
// since image and video responses are synchronous.
const omniMediaURL = "file:///tmp/dynamo_media"

// Where the omni stage configuration is written inside the worker pod.
const (
	omniStageDir    = "/etc/omni"
	omniStagePath   = omniStageDir + "/stage.yaml"
	omniStageVolume = "omni-stage"
	omniStageEnv    = "HANGAR_OMNI_STAGE_CONFIG"
)

// omniStageConfig is the stage-config JSON for a deployment with a diffusion block, or "" to keep
// omni's defaults. `devices` counts the pod's devices from zero, as the container sees them.
func omniStageConfig(in Inputs) (string, error) {
	d := in.Deployment.Serving.Diffusion
	if d == nil || (d.ModelClassName == "" && len(d.DefaultSamplingParams) == 0) {
		return "", nil
	}
	n := in.Layout.devices(in.Deployment)
	ids := make([]string, n)
	for i := range ids {
		ids[i] = strconv.Itoa(i)
	}
	stage := map[string]any{
		"stage_id":     0,
		"devices":      strings.Join(ids, ","),
		"max_num_seqs": 1,
	}
	if d.ModelClassName != "" {
		stage["model_class_name"] = d.ModelClassName
	}
	if len(d.DefaultSamplingParams) > 0 {
		stage["default_sampling_params"] = d.DefaultSamplingParams
	}
	body, err := json.Marshal(map[string]any{
		"async_chunk":                  false,
		"distributed_executor_backend": "mp",
		"stages":                       []any{stage},
	})
	if err != nil {
		return "", fmt.Errorf("encode omni stage config: %w", err)
	}
	return string(body), nil
}

// omniStageContainer writes the stage config into an emptyDir, so it is part of the hashed pod
// template and needs no ConfigMap lifecycle or RBAC.
func omniStageContainer(image, config string) corev1.Container {
	return corev1.Container{
		Name:                     "omni-stage-config",
		Image:                    image,
		Command:                  []string{"sh", "-c", `printf '%s' "$` + omniStageEnv + `" > ` + omniStagePath},
		Env:                      []corev1.EnvVar{{Name: omniStageEnv, Value: config}},
		TerminationMessagePolicy: corev1.TerminationMessageFallbackToLogsOnError,
		VolumeMounts:             []corev1.VolumeMount{{Name: omniStageVolume, MountPath: omniStageDir}},
	}
}

// omniArgs is the omni worker's command line, its own list because `dynamo.vllm.omni` exits on the
// chat worker's KV and cache flags. engine_args is laid over last.
func omniArgs(in Inputs, opts Options, stage bool) ([]string, error) {
	modality := in.Deployment.Serving.OutputModality
	if modality != OutputImage && modality != OutputVideo {
		return nil, fmt.Errorf("the omni engine needs an output modality of %q or %q, got %q",
			OutputImage, OutputVideo, modality)
	}
	model, isPath, err := modelRef(in.Artifact)
	if err != nil {
		return nil, err
	}
	if isPath && opts.LocalModelCacheDir != "" {
		if local, ok := localModelPath(model); ok {
			model = local
		}
	}
	derived := map[string]string{
		"model":               model,
		"served-model-name":   servedModelName(in),
		"output-modalities":   modality,
		"media-output-fs-url": omniMediaURL,
	}
	if stage {
		derived["stage-configs-path"] = omniStagePath
	}
	if v := in.Deployment.Serving.Quantization; v != nil && *v != "" {
		derived["quantization"] = *v
	}
	for key, value := range in.Deployment.Serving.EngineArgs {
		derived[strings.TrimPrefix(key, "--")] = value
	}
	withoutContentLogging(derived)
	keys := make([]string, 0, len(derived))
	for key := range derived {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	args := make([]string, 0, len(keys)*2)
	for _, key := range keys {
		args = append(args, "--"+key)
		if derived[key] != "" {
			args = append(args, derived[key])
		}
	}
	return args, nil
}

// kvTransferConfig is how the two roles move a KV cache between them: NixlConnector, with
// `kv_role: kv_both` on prefill and decode alike. The transfer mechanism is a property of the
// topology rather than of a role, which is why it is derived here and refused from a role's own
// arguments.
const kvTransferConfig = `{"kv_connector":"NixlConnector","kv_role":"kv_both"}`

// nixlBackendsFragment is the `backends` entry for NixlConnector's extra config, or "" for vLLM's
// default UCX, which reaches EFA no faster than TCP; EFA needs LIBFABRIC.
func nixlBackendsFragment(opts Options) string {
	if len(opts.NIXLBackends) == 0 {
		return ""
	}
	quoted := make([]string, 0, len(opts.NIXLBackends))
	for _, b := range opts.NIXLBackends {
		if b = strings.TrimSpace(b); b != "" {
			quoted = append(quoted, strconv.Quote(b))
		}
	}
	if len(quoted) == 0 {
		return ""
	}
	return `,"kv_connector_extra_config":{"backends":[` + strings.Join(quoted, ",") + `]}`
}

// nixlConnector renders one NixlConnector, with the cluster's backend choice applied.
func nixlConnector(opts Options) string {
	return `{"kv_connector":"NixlConnector","kv_role":"kv_both"` + nixlBackendsFragment(opts) + `}`
}

// offloadTier reads `offload: host` and `offload_gib` from prefix_cache_config. GiB is converted to
// vLLM's `cpu_bytes_to_use`, which is the whole instance's pool across TP ranks, not per rank.
func offloadTier(in Inputs) (gib int, on bool) {
	cfg := in.Deployment.Serving.PrefixCacheConfig
	if !strings.EqualFold(strings.TrimSpace(cfg["offload"]), "host") {
		return 0, false
	}
	// A tier that is on but unsized is a configuration error the engine would report as
	// `cpu_bytes_to_use must be specified`, several minutes into a load. Default it instead.
	gib = 512
	if n, err := strconv.Atoi(strings.TrimSpace(cfg["offload_gib"])); err == nil && n > 0 {
		gib = n
	}
	return gib, true
}

// roleKVTransferConfig is the connector stack for one role: NixlConnector, and with the offload
// tier vLLM's MultiConnector over OffloadingConnector and NixlConnector on prefill only, since
// decode never prefills. Not Dynamo's PdConnector, which rejects hybrid KV cache models.
func roleKVTransferConfig(in Inputs, opts Options, role workerRole) string {
	gib, on := offloadTier(in)
	if !on || role.Role != "prefill" {
		return nixlConnector(opts)
	}
	return fmt.Sprintf(
		`{"kv_connector":"MultiConnector","kv_role":"kv_both","kv_connector_extra_config":`+
			`{"connectors":[{"kv_connector":"OffloadingConnector","kv_role":"kv_both",`+
			`"kv_connector_extra_config":{"spec_name":"CPUOffloadingSpec","cpu_bytes_to_use":%d}},`+
			`%s]}}`,
		int64(gib)*1024*1024*1024, nixlConnector(opts),
	)
}

// aggregatedKVTransferConfig is the offload tier on a worker with no role: OffloadingConnector
// alone, or nothing when the tier is off.
func aggregatedKVTransferConfig(in Inputs) string {
	gib, on := offloadTier(in)
	if !on {
		return ""
	}
	return fmt.Sprintf(
		`{"kv_connector":"OffloadingConnector","kv_role":"kv_both","kv_connector_extra_config":`+
			`{"spec_name":"CPUOffloadingSpec","cpu_bytes_to_use":%d}}`,
		int64(gib)*1024*1024*1024,
	)
}

// kvEventsConfig makes a worker publish the blocks it holds for the frontend's KV router. Upstream
// publishes nothing unless this is set, whatever prefix caching says.
const kvEventsConfig = `{"publisher":"zmq","topic":"kv-events","endpoint":"tcp://*:20080","enable_kv_cache_events":true}`

// publishesKVEvents follows upstream: a worker publishes when it prefills with its prefix cache
// on, aggregated included; decode never does.
func publishesKVEvents(in Inputs, role workerRole) bool {
	if role.Role == "decode" {
		return false
	}
	// A prefill role's cache is on regardless of the workload's answer — see the exemption
	// `engineArgs` applies below — so only the aggregated worker consults the setting.
	return role.Role == "prefill" || in.Deployment.Serving.PrefixCacheEnabled
}

// engineArgs builds the engine command line: serving fields first, engine_args over them, so the
// escape hatch wins over a default.
func engineArgs(in Inputs, opts Options, role workerRole) ([]string, error) {
	model, isPath, err := modelRef(in.Artifact)
	if err != nil {
		return nil, err
	}
	// Only a path can be mirrored. A bare repo id is resolved by huggingface_hub against HF_HOME,
	// which still points at the shared cache, so there is no local directory to name.
	if isPath && opts.LocalModelCacheDir != "" {
		if local, ok := localModelPath(model); ok {
			model = local
		}
	}

	derived := map[string]string{
		"model":             model,
		"served-model-name": servedModelName(in),
		"max-model-len":     strconv.Itoa(in.Deployment.Serving.ContextLength),
	}
	// Only against a repo id. --revision is what huggingface_hub resolves the repo at, and the
	// path form has already resolved it — the revision is the last segment of the directory.
	// Sending both says the same thing twice and invites an engine to disagree with itself.
	if rev := hfRevision(in.Artifact); rev != "" && !isPath {
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

	// KV events on every role that prefills, unless engine_args set them. Adding the flag changes
	// the hashed worker spec and reloads weights once; without it the KV router indexes nothing.
	// Unlike `disaggregation-mode` and `kv-transfer-config` below, which the plane refuses from
	// engine_args, this is a default engine_args may override.
	if _, set := derived["kv-events-config"]; !set && publishesKVEvents(in, role) {
		derived["kv-events-config"] = kvEventsConfig
	}

	// An aggregated worker has no role and normally no transfer config. The offload tier is the
	// one thing that gives it one, so it is set here rather than inside the role branch below.
	if role.Role == "" {
		if cfg := aggregatedKVTransferConfig(in); cfg != "" {
			if _, set := derived["kv-transfer-config"]; !set {
				derived["kv-transfer-config"] = cfg
			}
		}
	}

	if role.Role != "" {
		derived["disaggregation-mode"] = role.Role
		derived["kv-transfer-config"] = roleKVTransferConfig(in, opts, role)
		if role.Spec != nil {
			if v := role.Spec.MaxBatchTokens; v != nil {
				derived["max-num-batched-tokens"] = strconv.Itoa(*v)
			}
			for key, value := range role.Spec.EngineArgs {
				derived[strings.TrimPrefix(key, "--")] = value
			}
		}
	}

	withoutContentLogging(derived)

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
	// vLLM caches prefixes by default, so only off needs saying. A disaggregated prefill worker
	// keeps its cache regardless: it is the only role whose cache can be hit, and the KV router's
	// events need it.
	if !in.Deployment.Serving.PrefixCacheEnabled && role.Role != "prefill" {
		args = append(args, "--no-enable-prefix-caching")
	}
	return args, nil
}

// contentLoggingFlags write a request's prompt or completion to the worker's log. Request content
// is not retained, so these are stripped even from engine_args; the plane refuses them too, and
// this covers rows written before it did.
var contentLoggingFlags = []string{"enable-log-requests", "enable-log-outputs", "log-requests", "log-requests-level"}

// withoutContentLogging deletes every content-logging flag from an assembled argument map, under
// any spelling the engine's parser would accept: leading dashes and `_` for `-` both name the same
// flag to vLLM and SGLang. A log level verbose enough to log request content (`logLevelFlags`) is
// deleted too, which leaves the engine at its default of info.
func withoutContentLogging(derived map[string]string) {
	for key, value := range derived {
		normal := strings.ReplaceAll(strings.TrimLeft(key, "-"), "_", "-")
		if slices.Contains(contentLoggingFlags, normal) {
			delete(derived, key)
		}
		if slices.Contains(logLevelFlags, normal) && isVerbose(value) {
			delete(derived, key)
		}
	}
}

// servedModelName is the model id clients send, as the plane resolved it; engine_args can still
// override it.
func servedModelName(in Inputs) string {
	return strings.TrimSpace(in.Deployment.ServedModelName)
}

// modelRef is what --model points at, and whether it is a path. A pinned HF source is served from
// its snapshot directory, since some engines open files under --model directly and a repo id
// would read as a relative path. Unpinned sources keep the repo id; sources with no HF ref use
// the cache's digest path.
func modelRef(art wire.ArtifactDesired) (ref string, isPath bool, err error) {
	repo := ""
	revision := ""
	for _, source := range art.Sources {
		if strings.HasPrefix(source.URI, "hf://") {
			if repo == "" {
				repo, revision = SplitHFRef(source.URI)
			}
		}
	}
	if repo != "" {
		if revision == "" {
			return repo, false, nil
		}
		return HFSnapshotDir(repo, revision), true, nil
	}
	if art.ArtifactID == "" {
		return "", false, fmt.Errorf("no artifact to serve")
	}
	return "/models/" + art.ArtifactID, true, nil
}

// HFSnapshotDir is where huggingface_hub lays down one revision of a repo in the shared cache. It
// must match hfScript's reconstruction, the writer of that directory.
func HFSnapshotDir(repo, revision string) string {
	return HFHome + "/hub/models--" + strings.ReplaceAll(repo, "/", "--") + "/snapshots/" + revision
}

// applyPodOverlay merges the serving block's pod configuration over the rendered spec by strategic
// merge, so containers and env merge by name instead of being replaced. checkOverlay has already
// limited it to the keys in podOverlayKeys.
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

// applyDynamoOverlay merges the serving block's configuration over the graph: maps merge, lists
// replace, since the CRD has no Go type to read merge keys from. checkOverlay has already limited
// it to annotations and envs, so it cannot replace spec.components.
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

// Labels recording which declared object a rendered one came from. An owner reference across
// namespaces reads as a deleted owner and gets the dependent collected.
const (
	OwnerNameLabel      = "hangar.dev/owner"
	OwnerNamespaceLabel = "hangar.dev/owner-namespace"
)

// Owned records which declared object a rendered one came from: an owner reference only within one
// namespace, labels always, and the reconciler's finalizer handles teardown across namespaces.
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

// SplitHFRef splits hf://owner/name@<sha> into repo id and revision, since huggingface_hub rejects
// '@' in a repo id.
func SplitHFRef(uri string) (repo, revision string) {
	repo = strings.TrimPrefix(uri, "hf://")
	if i := strings.LastIndex(repo, "@"); i >= 0 {
		return repo[:i], repo[i+1:]
	}
	return repo, ""
}

// hfRevision is the revision the preferred HF source pins, passed as --revision so the engine
// loads the commit the prefetch downloaded.
func hfRevision(art wire.ArtifactDesired) string {
	for _, source := range art.Sources {
		if strings.HasPrefix(source.URI, "hf://") {
			_, revision := SplitHFRef(source.URI)
			return revision
		}
	}
	return ""
}
