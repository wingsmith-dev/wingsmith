// Package render turns declared domain objects into the Kubernetes objects that serve them.
//
// This is the only package in the operator that knows what a DynamoGraphDeployment is or what a
// vLLM flag looks like; everything upstream deals in deployments, serving blocks and artifacts.
// Objects are built as unstructured rather than against NVIDIA's Go types, so the build does not
// track their release cadence, and they are applied server-side where the API server validates.
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

// HFHome is where the shared model cache is mounted, and therefore what HF_HOME is set to in every
// pod that reads or writes it.
//
// One constant because the download and the worker have to agree on it exactly: the prefetch job
// writes the weights under this root and the worker is pointed at a directory beneath it, so two
// literals that drifted apart would leave a job filling one path and a worker waiting on another,
// with nothing in either pod's log to say the two were different.
const HFHome = "/models/hf"

// LocalModels is where a node's own copy of the weights is mounted inside the worker.
//
// A second root rather than a mount over /models, because the fallback has to stay reachable: the
// init container reads the shared copy and writes the local one in the same filesystem namespace,
// and a worker whose mirror is a symlink back to /models needs both paths to resolve at once.
const LocalModels = "/models-local"

// localModelPath mirrors a path under /models onto the node-local root, preserving the layout.
//
// The layout is preserved rather than flattened so that a directory holding a repo's snapshot
// keeps the sibling files huggingface_hub and the engines expect to find next to it, and so that
// the two roots can be compared by eye when one of them is wrong.
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

	// AnthropicAPIEnabled serves the Anthropic Messages API (`/v1/messages`) beside the OpenAI
	// one. AnthropicStripPreamble removes a client-injected system-prompt prefix that varies per
	// request and carries no instructions for the model, so requests from the same client share a
	// prefix the cache can hit. Both are environment because Dynamo owns the frontend's command
	// line.
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

// The three annotations that say which inputs a rendered graph was built from.
//
// They exist so that a later pass can ask "is what is running already built from this?" without
// comparing the rendered object to the live one. That distinction is the whole point: a
// comparison of outputs makes every change to this renderer a change to every running workload,
// and a comparison of inputs does not. See `renderIsCurrent` in the reconciler.
//
// Between them they cover everything a render reads. Generation and the serving digest are the
// plane's — the plane bumps the generation on any declared change, including scale and the
// served model name, and the digest is its own hash of what decides the running process,
// artifact included. The layout is this cluster's, computed here by the capacity check and
// invisible to the plane, which is why it needs a third annotation rather than riding on either
// of theirs.
const (
	GenerationAnnotation    = "hangar.dev/generation"
	ServingDigestAnnotation = "hangar.dev/serving-digest"
	LayoutAnnotation        = "hangar.dev/layout"
)

// The rollout strategy Dynamo reads off `spec.annotations` of the graph deployment.
//
// Dynamo's defaults (25%/25%) round to surge 1 / unavailable 0 at small replica counts, which
// deadlocks on a GPU pool with no spare cards: the new pod is Pending on `Insufficient
// nvidia.com/gpu` while the old one may not be scaled down. Surge 0, unavailable 1 rolls in place,
// one worker at a time. Rendered for aggregated deployments too, since they have the same zero
// spare cards. A constant rather than a declared field because it is the only safe value until a
// cluster with spare capacity needs another.
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

// WorkerComponent is the Dynamo component name for the engine.
//
// One worker, aggregated: prefill and decode share an engine loop. Disaggregating them is a
// topology decision with a real trade — it buys latency isolation and pays for it in KV capacity,
// because each worker loads its own copy of the weights — and nothing on the serving block
// expresses that choice yet. Rendering it silently would be picking a side of that trade on the operator's own
// authority.
const WorkerComponent = "VllmDecodeWorker"

// PrefillComponent is the Dynamo component name for the prefill engine of a disaggregated
// deployment, beside `WorkerComponent` which stays the decode one.
//
// Declared here before anything renders it, because its length is the reason `maxWorkloadName`
// stopped being derived: it is one byte longer than `VllmDecodeWorker`, and under the old
// arithmetic merely naming it would have shortened the bound for every deployment in the fleet.
// `TestComponentBudget` holds it to the budget instead.
//
// The spelling is Dynamo's, not ours, for the reason `FrontendComponent` gives below: the name
// goes into the `nvidia.com/dynamo-component` label and into the Service selector derived from
// it, so it is matched on rather than read.
const PrefillComponent = "VllmPrefillWorker"

// FrontendComponent is the Dynamo component name for the OpenAI-compatible frontend.
//
// The string is not ours to choose. Dynamo puts a component's name in the `nvidia.com/dynamo-
// component` label and in the Service selector derived from it, and two things in this repository
// find a frontend by that label rather than by name — the serving agent's resolver and this
// operator's own benchmark executor — because the name is the part that varies between clusters
// while the label does not. Renaming this breaks both, and it breaks them as a lookup that finds
// nothing rather than as a compile error.
const FrontendComponent = "Frontend"

// Disaggregated says whether a deployment's roles are separate processes.
//
// Reads the topology rather than the presence of roles, and both have to agree before anything
// renders differently: a plane too old to send either leaves both empty, and empty must render
// exactly what it rendered before this existed. The plane refuses the disagreeing cases at
// declaration, so this is the last cheap check rather than the only one.
func Disaggregated(dep wire.DeploymentDesired) bool {
	return dep.Serving.Topology == "disaggregated" && len(dep.Serving.Roles) > 0
}

// workerRole is one worker component to render: which Dynamo component it is, how many of it, and
// the per-role configuration that makes its process different from the other role's.
//
// One descriptor rather than a second copy of the render keyed on three constants. The aggregated
// case is a single descriptor with an empty Role and a nil Spec, which is what keeps its output
// byte-identical: every disaggregated addition below is guarded on Role being non-empty, so there
// is one code path and the aggregated branch of it renders what it always did.
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

// workerRoles is the set of worker components this deployment renders, in a stable order.
//
// Ordered by the wire's own role list rather than sorted, because the plane owns the order and a
// re-ordering here would change the rendered object without changing anything about it. Dynamo
// hashes the components together (see `specHashPlaceholder`), so a spurious re-order is a rename,
// and a rename is a weight reload.
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

// roleComponent maps a declared role onto the Dynamo component that serves it.
//
// Closed, and an unknown role is an error rather than a default. A role this operator does not
// know renders a `--disaggregation-mode` no engine accepts, which is a worker that will not start
// — and the plane has already refused anything outside the set, so reaching here means the two
// disagree and guessing would hide that.
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

// DynamoGraphDeployment renders one deployment into the object that serves it.
//
// The graph holds the model's workers and, where the cluster has opted in, the frontend the model
// is served through: one per model, so a model that needs a different frontend build is that
// deployment's business alone. The gateway has already resolved a request to a deployment, and
// the in-cluster agent picks the frontend by label, so nothing routes on the request body.
func DynamoGraphDeployment(in Inputs, opts Options) (*unstructured.Unstructured, error) {
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
	if opts.FrontendEnabled {
		if err := checkFrontendSurvivedOverlay(obj, in); err != nil {
			return nil, err
		}
	}
	if err := checkImages(obj.Object, opts); err != nil {
		return nil, fmt.Errorf("deployment %s: %w", in.Deployment.DeploymentID, err)
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
// The binding limit is the DynamoComponentDeployment Dynamo names after the graph, whose name is
// also the value of the `nvidia.com/selector` label on the Deployment beneath it: a label value is
// 63 bytes, and a child over it is refused by the API server on an object this operator never
// reads, leaving the deployment at 0 ready replicas with no reason given.
//
// A chosen number, not one derived from the component list: deriving it made adding a component
// shorten the bound and rename (and reload) every deployment at the old length.
// `TestComponentBudget` holds the components to it instead. Do not re-derive it.
const maxWorkloadName = 32

// componentNameBudget is what a component name may spend: what is left of the 63 bytes after the
// workload name, two dashes and the eight-character spec hash. `VllmPrefillWorker` (17) is the
// longest this renderer emits; a component past the budget fails `TestComponentBudget`.
const componentNameBudget = maxObjectNameBytes - maxWorkloadName - len("-") - len("-") - len(specHashPlaceholder)

// maxObjectNameBytes is the label-value limit the derived name is actually held to.
const maxObjectNameBytes = 63

// specHashPlaceholder stands in for the hash Dynamo derives for worker components. Only its
// length is used.
//
// Dynamo's `ComputeDGDWorkersSpecHash` hashes every worker component of a graph together, so both
// roles of a disaggregated deployment carry the same suffix and resolve to the same runtime
// namespace, which is how the frontend pairs prefill with decode. Rendering the roles as two
// graphs would put them in two namespaces and quietly stop disaggregation. Replicas are excluded
// from the hash, so a ratio change costs nothing.
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
// The role label is added only for a disaggregated deployment, and that conditional is the same
// one every other addition in this file makes: an aggregated deployment's pod template has to stay
// byte-identical to what it was, because the template is what Dynamo hashes into the object name
// and a new label would rename and reload every worker in the fleet.
//
// Ingestion reads it back to say which role a replica is, which is the whole reason it is a label
// rather than something derived from the component name at read time: `RoleLabel` is one string
// this operator writes and the observed path matches on, where a name is a convention two sides
// would have to keep agreeing about.
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

// localizeScript mirrors one model directory onto the node's own disk before the engine starts.
//
// Written as a shell script rather than a Go loop because it runs where the weights are, and the
// only thing it needs is to be readable by whoever is standing in front of a worker that started
// slowly and wants to know which copy it read.
//
// The contract with the engine is that $DST always resolves to the weights afterwards. It is a
// real directory when the mirror succeeded and a symlink to the shared copy when it did not, so
// the worker's --model argument is the same string either way and no argument has to be rendered
// conditionally on something only the node knows.
//
// Failure is a slow start and never a broken one, which is what makes this safe to turn on ahead
// of the disk being right. A node with no room, a read error part-way through, an interrupted
// previous attempt: each ends at the symlink, and the engine reads over NFS exactly as it did
// before this existed.
//
// $DST/.hangar-complete is the marker, written last. Its absence is what makes a partial copy
// worthless rather than dangerous — a mirror interrupted by a spot reclaim is discarded and
// redone rather than served as if it were whole, and a half-written tensor file is the kind of
// corruption that surfaces as garbage output rather than as an error.
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

// localizeContainers is the init container that fills the node-local mirror, or nothing.
//
// It runs on the engine image so that the copy sees the same filesystem the engine will, and it
// asks for no accelerator: this is a file copy, and a pod that requested a GPU for it would hold
// eight of them idle for the length of the download.
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

// frontendLocalizeScript copies a model's metadata, and deliberately not its weights.
//
// The frontend materializes a model card by opening the path the worker advertised. Once the
// worker advertises the node-local mirror, the frontend has to be able to open that same string --
// but the frontend runs on ordinary nodes with no mirror on them and no reason to hold weights it
// will never read. `ignore_weights` is already true on its side; the tokenizer and the configs are
// all it wants, and they are a few megabytes.
//
// So this is the same path with none of the bulk, into an emptyDir rather than the host disk: it
// is per-pod scratch, it cannot be mistaken for a real mirror by a worker that later lands on the
// node, and it writes no `.hangar-complete`.
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

// frontendLocalizeContainers gives the frontend the path the workers advertise.
//
// Without it the frontend reads `/models-local/...` as a Hugging Face repo id, asks the Hub for a
// model of that name, gets a 404 and retries forever -- so no model card is ever published, and
// every request to a fleet of perfectly healthy workers is answered with an empty model list.
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

// localModelVolume is the node's own disk, or nothing.
//
// A hostPath and not an ephemeral volume, because the point is to outlive the pod: a mirror that
// were wiped on every restart would pay the copy every time and never once collect on it, which
// is strictly worse than reading from the shared cache.
//
// DirectoryOrCreate so that a node which has never held a model is not a scheduling failure.
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

// workerProbes replace Dynamo's defaults, which kill a busy worker rather than a broken one.
//
// Dynamo renders liveness with failureThreshold 1 and a four-second timeout, and the workers'
// /live reports the generate endpoint's health, which is 503 both while a worker is saturated and
// while it loads. One slow answer under load would SIGKILL a worker holding a multi-hundred-GB
// model. Readiness stays quick, since leaving rotation is cheap and reversible; liveness allows
// three minutes, which still catches a wedged worker long before the reload it causes. The
// startup probe is left to Dynamo.
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

// fabricSecurityContext widens a fabric worker so NIXL can register GPU memory with the NIC:
// IPC_LOCK for pinned memory, and privileged where the cluster says its fabric needs it (EFA
// does, for fi_mr_reg against CUDA memory). A privileged container is not isolated from its
// node, which is why it is opt-in per cluster and limited to GPU workers that own their node.
// Delete the privileged half once NIXL registers through DMA-BUF instead.
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

	// A host KV offload pool is the one allocation here that is both large and unbounded by the
	// GPU, so it is the one that needs a ceiling: without a limit the pool grows into the node
	// and the kernel picks a victim, which on a node running one worker per eight GPUs means the
	// OOM killer takes the thing we are measuring.
	//
	// Derived from the pool rather than declared, so the number has a reason. The limit is the
	// pool plus the declared request -- the pool is what the connector adds on top of everything
	// the worker already needed, and the request is the best available statement of that. A
	// deployment that turned the tier on without declaring memory gets no limit, because a
	// ceiling computed from an unknown floor is a guess, and an OOMKill mid-benchmark is worse
	// than an unbounded pool we are watching.
	//
	// Only when the tier is on: every other worker in the fleet keeps rendering exactly as before,
	// which keeps a change made for one experiment from becoming a fleet-wide OOMKill risk.
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
			// What an engine says on its way out, kept where the plane can read it.
			//
			// The default policy reads /dev/termination-log, which nothing here writes, so a
			// container that died of `unrecognized arguments: --tool-call-parser glm47` left
			// Kubernetes holding `exitCode: 2` and nothing else. `containerTrouble` reported
			// `CrashLooping` with the words "back-off restarting failed container", and the
			// sentence that named the flag existed only in `kubectl logs --previous` on a
			// cluster nobody outside it can reach.
			//
			// FallbackToLogsOnError has the kubelet copy the tail of the container's own
			// output into `lastState.terminated.message` whenever it exits non-zero, which is
			// the field the diagnosis already reads. It costs nothing on a container that
			// exits cleanly and is the difference between "a replica is repeatedly crashing"
			// and knowing which argument to remove.
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

// frontendComponent renders the frontend this model is served through.
//
// The pod template carries ownershipLabels, and that is load-bearing: Dynamo copies a component's
// pod-template labels onto the Service it derives, and `hangar.dev/deployment-id` on that Service
// is how the in-cluster agent finds this frontend for a request already resolved to this
// deployment. Without them the deployment is reachable by nothing.
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

// frontendPodSpec is what one frontend pod is.
//
// Not podSpec: the GPU node selector and toleration belong on the engine, and a frontend that
// tolerated the GPU taint could occupy an accelerator node while holding no weights. No command
// or arguments either: Dynamo derives the frontend's command line from `type: frontend`, so its
// settings are environment. The serving block's pod overlay is not applied, because it is written
// about the engine container and both are called `main`.
//
// The model cache is mounted read-only because the worker advertises its model as a path (see
// modelRef) and the frontend resolves the model card from that same path to build its tokenizer.
// A frontend that cannot open it asks huggingface.co for a repo named after the path, gets a 404
// and serves an empty model list while passing every probe.
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
				// The same fixed seed the worker gets, and it is worth nothing unless it
				// is the same on both. KV-aware routing decides which worker already
				// holds a prefix by hashing block contents; Python salts hashes per
				// process, so a frontend and a worker that disagree on the salt hash
				// identical blocks differently and miss every cache hit — silently, as a
				// performance loss nothing reports.
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

// frontendRouterEnv is the router configuration a frontend is started with, as environment.
//
// `DYN_ROUTER_MODE` is always rendered, `kv` when nothing is declared. Upstream's default is
// round-robin, which at two or more workers sends alternate turns of a conversation to a worker
// that has never seen it, re-prefilling the whole context and halving the fleet's prefix hit rate.
// It does not depend on the replica count, since a mode that moved with an autoscaled count would
// roll the frontend on a scale event. With one worker the KV router picks the only candidate.
//
// The other variables render only when declared, in a fixed order, because Dynamo hashes the
// frontend's pod template and an added variable rolls the frontend. KV mode is half of a pair; the
// other half is `kvEventsConfig` on the worker. The plane has already refused values Dynamo would
// reject; these checks are the last cheap ones.
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

// routerFloat spells a router number the way Python writes a float: `1.0`, never `1`.
//
// Dynamo parses either. The spelling is fixed because it is part of the pod template Dynamo hashes,
// and it is Python's because that is how the values were first set by hand on a running frontend,
// so declaring the same numbers renders the same template instead of rolling it.
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

// archLabel is the well-known node label for a node's CPU architecture.
//
// Pinned to amd64 on an omni worker because vLLM-Omni has no arm64 build: on a Grace node the
// image pulls and the container dies on its first instruction. The plane refuses an omni
// workload on a Grace device class before it is placed; this is the cluster's own half, and
// because the capacity check reads the same selector, a cluster with only arm64 GPU nodes
// reports no capacity rather than rendering a pod that cannot run.
const archLabel = "kubernetes.io/arch"

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
//
// Closed, and an unknown engine is an error. It used to fall through to `vllm`, which was a
// harmless guess while every engine served chat. With omni beside it, a guess is a workload that
// comes up healthy answering the wrong API, so a name this operator has never heard of fails the
// render and says so.
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

// OmniComponent is the Dynamo component name for a vLLM-Omni worker, and `worker` its type.
//
// A diffusion worker has no prefill/decode split. `type` is not decorative: Dynamo derives port
// mapping, frontend detection and the `nvidia.com/dynamo-component-type` label from it. Nothing
// outside this file matches on a worker's component name, so other code follows this constant.
const (
	OmniComponent = "OmniWorker"
	omniType      = "worker"
)

// Output modalities an omni worker can be told to emit.
const (
	OutputImage = "image"
	OutputVideo = "video"
)

// omniMediaURL is where the omni worker writes what it generates before returning it inline.
//
// A pod-local path because image and video are synchronous today: the frontend answers with the
// bytes, and nothing reads the directory afterwards. Video jobs (plan 07) will point this at
// object storage, which is the whole of the worker half of that change.
const omniMediaURL = "file:///tmp/dynamo_media"

// Where the omni stage configuration is written inside the worker pod.
const (
	omniStageDir    = "/etc/omni"
	omniStagePath   = omniStageDir + "/stage.yaml"
	omniStageVolume = "omni-stage"
	omniStageEnv    = "HANGAR_OMNI_STAGE_CONFIG"
)

// omniStageConfig is the stage-config file for a deployment that declares a diffusion block, or
// "" when it declares none and omni's own defaults stand.
//
// JSON, which YAML parses. The fixed parts are one stage, synchronous chunks, the multiprocessing
// executor and one sequence at a time; `devices` is every device the pod holds, counted from zero,
// because that is how the container sees them.
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

// omniStageContainer writes the stage configuration into a volume the worker reads it from.
//
// An init container over an emptyDir, and not a ConfigMap. A ConfigMap is a second object with
// its own lifecycle — this operator has no RBAC for one, would have to apply it, name it by
// content so an edit reaches the pod, and prune the old ones — while this is part of the pod
// template, so Dynamo's spec hash covers the configuration and an edit rolls the worker like any
// other serving change. The worker's own image does the writing, so there is nothing new to pull.
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

// omniArgs is the omni worker's command line.
//
// Its own list rather than engineArgs with flags removed: `dynamo.vllm.omni` takes none of the KV,
// context or prefix-cache flags the chat worker is built around, and exits on any of them. It
// takes the model, the served name, the output modality, the media URL, the stage-config path and
// `--quantization`. engine_args is laid over last, as everywhere else.
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

// nixlBackendsFragment is the `backends` entry for a NixlConnector's extra config, or "" to let
// vLLM choose.
//
// vLLM defaults to `["UCX"]`. On an EFA fabric that is the wrong plugin: EFA is a libfabric
// provider, and UCX reaches it only through a path that moves KV no faster than TCP. Mounting the
// fabric is necessary and not sufficient; NIXL has to be told to use LIBFABRIC. Empty keeps vLLM's
// default, so a cluster with no fabric renders as before.
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

// offloadTier reads the host-memory KV offload switch out of the workload's prefix_cache_config.
//
// `offload: host` turns it on and `offload_gib` sizes it; anything else is off. The field already
// runs plane -> DB -> digest -> wire and was ignored here, so turning the tier on is a workload
// edit and not a release — which is what makes it possible to measure with and without it on the
// same hardware in one night.
//
// Sizing is in GiB because that is the unit the person choosing it thinks in, and it is converted
// to the bytes vLLM demands here rather than in the workload. `cpu_bytes_to_use` is the whole
// instance's pool across every TP rank, not per rank -- `CPUOffloadingSpec` multiplies the
// per-worker block size by world_size before dividing (kv_offload/cpu/spec.py:88-99) -- so the
// number means what a reader expects it to mean and is not silently multiplied by eight.
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

// roleKVTransferConfig is the connector stack for one role.
//
// Without the offload tier, both roles get NixlConnector. With it, prefill gets vLLM's
// `MultiConnector` wrapping `OffloadingConnector` and `NixlConnector`, and decode is left alone:
// prefill is the only role whose cache can be hit, since the next turn resends this turn's
// context, and decode never prefills.
//
// vLLM's `MultiConnector` rather than Dynamo's `PdConnector`, because `PdConnector` limits its
// first connector to ones without hybrid KV cache manager support, which models with SSM layers
// require. `blocks_per_chunk` is left at its default because `block_size` must divide the
// engine's block size, which nothing here sets.
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

// aggregatedKVTransferConfig is the offload tier on a worker that has no role.
//
// An aggregated worker prefills and decodes in one process, so `OffloadingConnector` stands alone
// with no transport to compose with, and an aggregated deployment renders no
// `kv-transfer-config` at all unless the tier is on.
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

// kvEventsConfig makes a worker publish which blocks it holds, which is what the frontend indexes
// to route a conversation back to the worker that already prefilled it.
//
// Explicit, because upstream's `create_kv_events_config` returns nil unless it is set: enabling
// prefix caching is not enough, whatever the flag's help text suggests. A KV-mode frontend with no
// events scores every worker at zero overlap and degrades to load balancing while still reporting
// itself as a KV router.
const kvEventsConfig = `{"publisher":"zmq","topic":"kv-events","endpoint":"tcp://*:20080","enable_kv_cache_events":true}`

// publishesKVEvents says whether this role should advertise the blocks it holds.
//
// Mirrors upstream's rule: a worker publishes when it prefills and its prefix cache is on.
// Aggregated workers prefill too, so they publish; leaving them out leaves multi-replica
// aggregated models routing cache-blind. A disaggregated decode role never prefills and upstream
// refuses the config there.
func publishesKVEvents(in Inputs, role workerRole) bool {
	if role.Role == "decode" {
		return false
	}
	// A prefill role's cache is on regardless of the workload's answer — see the exemption
	// `engineArgs` applies below — so only the aggregated worker consults the setting.
	return role.Role == "prefill" || in.Deployment.Serving.PrefixCacheEnabled
}

// engineArgs builds the engine command line from the deployment's serving block.
//
// The serving fields are rendered first and engine_args second, so an explicit argument wins over
// the derived one. That direction is deliberate: engine_args is the escape hatch for a flag this
// operator has never heard of, and an escape hatch that loses to a default is not one.
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

	// The role's own layer, above the deployment's and below nothing. Guarded on Role rather than
	// on Spec so that the aggregated command line is assembled exactly as it was: no new key, no
	// changed order, and therefore the same pod template and the same object name.
	//
	// `disaggregation-mode` and `kv-transfer-config` are derived here and refused from engine_args
	// by the plane, which is what makes them derived rather than defaulted. They come from the
	// topology — a decision already made, validated and placed against — and the operator lays
	// engine_args over its derived flags last, so a value arriving from anywhere else would not be
	// overruled by this, it would overrule it.
	// The events the frontend's KV router indexes, on every role that prefills — the aggregated
	// worker included, which is why this sits outside the role guard below rather than inside it.
	//
	// It is the half of the pair that costs something. Unlike the frontend's `DYN_ROUTER_MODE`,
	// this lands in the worker's argument list, and `ComputeDGDWorkersSpecHash` covers the worker
	// components, so adding it renames the worker Deployment and its pods reload their weights
	// once. That is a real bill and it is paid knowingly: without the events, KV mode on the
	// frontend indexes an empty tree and buys nothing at all.
	//
	// Defaulted and not derived, which is the difference between this flag and the two below it.
	// The plane refuses `disaggregation-mode` and `kv-transfer-config` from engine_args
	// (`TOPOLOGY_FLAGS`) and refuses neither this one nor anything else, so a workload is free to
	// send its own endpoint or topic and this must not overrule it — engine_args is the escape
	// hatch, and an escape hatch that loses to a default is not one. The deployment's own
	// engine_args are already merged by the time this runs, so the guard is what lets them win;
	// a role's engine_args are merged after and win by overwriting.
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
	// vLLM enables prefix caching by default, so only the off case needs saying.
	//
	// **A disaggregated prefill worker is exempt, and that exemption is the whole warm path.**
	// Prefill is the only role whose prefix cache can be hit: turn N+1 of a conversation resends
	// turn N's context, and if the worker that prefilled it still holds those blocks the engine
	// skips them. Decode never prefills, so its prefix cache buys nothing and costs KV blocks —
	// which is why NVIDIA's own `disagg-kv-router` recipe puts `--no-enable-prefix-caching` on
	// the decode worker alone and leaves prefill's caching on. Copying that flag onto both roles,
	// which is what a uniform render does, forecloses the reuse: for the agentic shape we are
	// sizing for — a 30k prompt growing 1.3k a turn over 25 turns — it is the difference between
	// ~1.2M tokens prefilled per conversation and ~67k.
	//
	// It also disables KV-aware routing by a second route, because the events the router indexes
	// are only published when prefix caching is on. See `kvEventsConfig`.
	if !in.Deployment.Serving.PrefixCacheEnabled && role.Role != "prefill" {
		args = append(args, "--no-enable-prefix-caching")
	}
	return args, nil
}

// contentLoggingFlags are the engine flags that write a request's prompt or its completion to the
// worker's log: vLLM's `--enable-log-requests` and `--enable-log-outputs`, and SGLang's
// `--log-requests` and its level.
//
// Zero retention of request content is a promise made about the whole serving path, and a worker's
// stdout is collected, shipped and kept by whatever the cluster's log pipeline is — with a retention
// and an audience nobody agreed to for a caller's prompt. Both engines default these off, so the
// only way one reaches a command line is engine_args, which is the escape hatch and wins over
// everything else here. This is the one thing it does not win over: the plane refuses these keys on
// a workload, and this removes them again for any row written before it did.
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

// servedModelName is the model id an API client sends, as the plane resolved it.
//
// Nothing is derived here and there is nothing left to choose between: the plane collapses the
// workload's own declaration and the artifact's slug before it sends, so the field is never empty
// on the wire and this renders the string it is told. `engine_args["served-model-name"]` still
// beats it, because engine_args is laid over the derived flags.
func servedModelName(in Inputs) string {
	return strings.TrimSpace(in.Deployment.ServedModelName)
}

// modelRef is what --model is pointed at, and whether that is a path rather than a repo id.
//
// A pinned Hugging Face source is served from the directory the prefetch job already wrote it to,
// and not by repo id. Both forms resolve to the same bytes in the shared cache — HF_HOME is the
// cache volume, so a repo id costs no second copy — but only one of them works everywhere. A repo
// id is resolved by huggingface_hub, and an engine is free to reach the weights without it:
// GLM-5.3-Flash's own processor in Z-AI's vLLM build opens `<--model>/processor_config.json`
// directly, so a repo id arrives at the kernel as a relative path and the worker dies on
//
//	FileNotFoundError: 'zai-org/GLM-5.3-Flash/processor_config.json'
//
// A directory is what every engine agrees on, and it costs nothing to give: the path is derived
// from the same repo and revision the prefetch job derived it from, so the two cannot disagree
// about where the weights are without disagreeing about which weights they are.
//
// A source with no pinned revision keeps the repo id, because there is no snapshot directory to
// name — huggingface_hub resolves the branch head to a commit at download time and this renderer
// cannot know which. An artifact with no HF source at all falls back to the digest path the cache
// writes to, which was already a path and is why the path shape needs no engine support that the
// object-store case did not already need.
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

// HFSnapshotDir is where huggingface_hub lays one revision of one repo down under the shared cache.
//
// The layout is reconstructed rather than asked for, because the only thing that could answer is a
// download that has already finished, and this renders the pod that waits on it. It is the same
// reconstruction hfScript makes to watch its own progress, and it has to stay the same one: these
// two are the writer and the reader of a single directory, and a layout change upstream must break
// them together rather than leave a worker reading an empty path a job filled in elsewhere.
func HFSnapshotDir(repo, revision string) string {
	return HFHome + "/hub/models--" + strings.ReplaceAll(repo, "/", "--") + "/snapshots/" + revision
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
