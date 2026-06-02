package reconcile

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
	"github.com/hangar-dev/hangar/libs/operator/internal/workload"
)

// SyncedResourceReconciler converges the durable local copy of declared state onto the cluster.
//
// One controller over one CRD rather than one per domain kind. The kinds are not independent —
// a deployment cannot render without its artifact and those weights already being resident — so
// splitting them into separate controllers would buy separation and immediately spend it on
// cross-controller lookups for the same objects.
type SyncedResourceReconciler struct {
	Client client.Client
	Scheme *runtime.Scheme
	Log    *slog.Logger

	// Namespace is where the durable copies of declared state live.
	Namespace string

	// Render carries the per-cluster conventions a render needs and the plane does not declare.
	Render render.Options

	// ClusterID is this cluster's identity in the plane, carried on every frame reported upward.
	ClusterID string

	// Report sends this loop's own conclusions to the hub. Optional: a nil Reporter reconciles
	// exactly as before and tells the plane nothing, which is what the tests and any read-only
	// deployment want.
	Report Reporter

	// Devices says which accelerators a pod holds. Optional: left nil, no allocation is ever
	// reported, which is the honest answer — the binding comes from the exporters and a loop
	// that cannot read them does not know what any replica is holding.
	Devices DeviceIndex

	// Capabilities says what the cluster was observed to be able to do. Optional: left nil, the
	// gates fall back to the configured flags alone, which is what the tests want.
	Capabilities Capabilities

	reportMu sync.Mutex
	reported map[string]reportKey
	staged   map[string]stageState
	holding  map[string]map[string]wire.AllocationObserved
}

// DeviceIndex is the half of the observation loop's device binding this loop needs.
//
// Narrowed to one method for the same reason Reporter is narrowed: this loop needs an answer, not
// a scraper, and taking the interface keeps the exporter's scheduling out of the reconcile path.
type DeviceIndex interface {
	// DevicesForPod is the UUIDs of the accelerators one pod holds, or nil when unknown.
	DevicesForPod(namespace, name string) []string
}

// Capabilities is the half of the observation loop's cluster reading this loop gates on.
type Capabilities interface {
	// Multinode says a leader-worker backend was seen in the cluster's API groups.
	Multinode() bool
}

// multinodeEnabled says whether this cluster can render a replica that spans machines.
//
// Observation first, configuration as an override that can only widen. Detection is the honest
// answer — the CRDs are either served or they are not — and it keeps working when somebody
// installs a backend without touching this operator's values. The configured flag stays because
// detection matches on group name, so a backend named something unanticipated can still be
// asserted by hand; it cannot turn the capability off, because a cluster that demonstrably has a
// backend is not made incapable by a stale value.
func (r *SyncedResourceReconciler) multinodeEnabled() bool {
	if r.Render.MultinodeEnabled {
		return true
	}
	return r.Capabilities != nil && r.Capabilities.Multinode()
}

// +kubebuilder:rbac:groups=placeholder.hangar.dev,resources=syncedresources,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=placeholder.hangar.dev,resources=syncedresources/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=coordination.k8s.io,resources=leases,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch
// +kubebuilder:rbac:groups="",resources=nodes,verbs=get;list;watch;patch;update
// +kubebuilder:rbac:groups="",resources=pods/eviction,verbs=create
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch
// +kubebuilder:rbac:groups=nvidia.com,resources=dynamographdeployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=nvidia.com,resources=dynamographdeployments/status,verbs=get
// +kubebuilder:rbac:groups=nvidia.com,resources=dynamocomponentdeployments,verbs=get;list;watch;delete
// +kubebuilder:rbac:groups=batch,resources=jobs,verbs=get;list;watch;create;delete
// +kubebuilder:rbac:groups=batch,resources=jobs/status,verbs=get

// Reconcile implements the reconcile loop.
func (r *SyncedResourceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var res v1alpha1.SyncedResource
	if err := r.Client.Get(ctx, req.NamespacedName, &res); err != nil {
		if apierrors.IsNotFound(err) {
			// Gone. Drop what was last reported for it, so an object recreated at the same id
			// is not mistaken for a repeat of the state it was in before it was deleted.
			r.forgetReported(nameToID(req.Name))
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// Teardown runs before every other branch, including the authority gate. A resource being
	// deleted has no desired state left to converge on, and the one thing still owed to it is
	// the removal of what it rendered — which is this operator's job now that the rendered
	// object no longer carries an owner reference back to it.
	if !res.DeletionTimestamp.IsZero() {
		return r.teardown(ctx, &res)
	}

	// The authority gate runs before the staleness check, not after. A resource whose revision
	// has already been observed still has to be re-examined, because the thing that drifts is
	// the cluster object and not the desired state describing it.
	//
	// Observed is the closed direction and it is checked by value, not by absence: an empty
	// Authority is observed, so a plane that predates the field — or a payload that failed to
	// decode into one — writes nothing to this cluster. Managed is the only value that opens the
	// gate, and it has to be said out loud (ADR 0009).
	if res.Spec.Authority != v1alpha1.AuthorityManaged {
		if res.Spec.Adopt != nil {
			return r.reconcileAdopted(ctx, &res)
		}
		if res.Spec.Kind == "deployment" {
			return r.observed(ctx, &res)
		}
	}

	// Every kind is re-examined on every pass rather than skipped once its revision has been
	// observed. Desired state is only one of the inputs: a deployment also depends on an
	// artifact that may have arrived since, on weights that may have finished downloading since, and on
	// a rendered object that something else may have edited since. Gating on the revision alone
	// would make all three invisible until the plane happened to write again.

	switch res.Spec.Kind {
	case "deployment":
		return r.reconcileDeployment(ctx, &res)
	case "cache":
		return r.reconcileCache(ctx, &res)
	case "artifact":
		// Nothing to converge on its own. It is read by the deployment path, and an edit to it
		// re-triggers that path through the watch below rather than doing work here.
		return r.observed(ctx, &res)
	case "component":
		// Cluster-level dependencies — GPU operator, device plugin, storage, ingress. Driving
		// these to a declared version is a Helm release rather than an object to apply, which
		// is long-running and failable in a way an apply is not. It is deliberately not done
		// here yet.
		return r.observed(ctx, &res)
	default:
		return r.fail(ctx, &res, "UnknownKind", fmt.Sprintf("no reconciler for kind %q", res.Spec.Kind))
	}
}

// observed records a declared object this controller has nothing to apply for.
func (r *SyncedResourceReconciler) observed(ctx context.Context, res *v1alpha1.SyncedResource) (ctrl.Result, error) {
	if res.Status.ObservedRevision == res.Spec.Revision && res.Status.Phase == "Accepted" {
		return ctrl.Result{}, nil
	}
	patch := client.MergeFrom(res.DeepCopy())
	res.Status.ObservedRevision = res.Spec.Revision
	res.Status.Phase = "Accepted"
	setCondition(res, metav1.ConditionTrue, "NoClusterWork", "held as declared state; nothing to apply")
	r.report(res, phasePending, "NoClusterWork", "held as declared state; nothing to apply")
	return ctrl.Result{}, r.Client.Status().Patch(ctx, res, patch)
}

// SetupWithManager wires the reconciler to the manager.
//
// Watching the rendered objects is what closes the loop in the other direction: a graph deployment
// whose status changes, or a prefetch job that finishes downloading, wakes the declared object it
// came from without anything having to poll for it.
//
// By label rather than by ownership. Both rendered kinds live in the workload namespace and the
// declared copies live in this operator's own, and controller-runtime's ownership handler — like
// the garbage collector it mirrors — resolves an owner within the dependent's namespace. Owning
// them across that boundary linked nothing in this direction and actively destroyed the objects
// in the other.
func (r *SyncedResourceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	graph := &unstructured.Unstructured{}
	graph.SetGroupVersionKind(render.GVK)

	back := handler.EnqueueRequestsFromMapFunc(r.declaredFor)

	return ctrl.NewControllerManagedBy(mgr).
		For(&v1alpha1.SyncedResource{}).
		Watches(&batchv1.Job{}, back).
		Watches(graph, back).
		Named("syncedresource").
		Complete(r)
}

// declaredFor maps a rendered object back to the declared one it came from.
//
// The owner labels are the only link across the namespace boundary, and an object carrying neither
// is not ours — something else in the workload namespace, or one rendered before these labels
// existed. Either way the answer is to enqueue nothing rather than to guess a name from the
// object's own.
func (r *SyncedResourceReconciler) declaredFor(_ context.Context, obj client.Object) []ctrl.Request {
	labels := obj.GetLabels()
	name := labels[render.OwnerNameLabel]
	namespace := labels[render.OwnerNamespaceLabel]
	if name == "" || namespace != r.Namespace {
		return nil
	}
	return []ctrl.Request{{NamespacedName: types.NamespacedName{Namespace: namespace, Name: name}}}
}

// reconcileAdopted handles desired state promoted from a workload that was already running.
//
// It is the one place in this operator that is allowed to say no. An adopted object is not applied
// and never will be by this path: the plane promised it matches what is live, and the only useful
// thing to do is check that promise and refuse when it is broken. Converging instead would replace
// a serving pod template with a rendered one and reload every model behind it — the exact cost the
// whole adoption path exists to avoid.
//
// Refusing is not an error. Returning one would requeue with backoff and log a failure every time,
// when what actually happened is that a human has to look at a workload that drifted. The refusal
// goes on status, where it is visible and where the sync loop ships it upward.
func (r *SyncedResourceReconciler) reconcileAdopted(ctx context.Context, res *v1alpha1.SyncedResource) (ctrl.Result, error) {
	adopt := res.Spec.Adopt
	live, err := r.liveFingerprint(ctx, adopt)

	patch := client.MergeFrom(res.DeepCopy())
	condition := metav1.Condition{
		Type:               "Ready",
		ObservedGeneration: res.Generation,
		LastTransitionTime: metav1.Now(),
	}
	switch {
	case err != nil:
		res.Status.Phase = "AdoptionUnverified"
		condition.Status = metav1.ConditionFalse
		condition.Reason = "LiveObjectUnreadable"
		condition.Message = fmt.Sprintf("cannot read %s/%s %s: %v", adopt.Namespace, adopt.Name, adopt.Kind, err)
	case live != adopt.SpecFingerprint:
		res.Status.Phase = "AdoptionDrifted"
		condition.Status = metav1.ConditionFalse
		condition.Reason = "FingerprintMismatch"
		condition.Message = fmt.Sprintf(
			"%s/%s digests to %s but was adopted at %s; refusing to reconcile — converging would restart it",
			adopt.Namespace, adopt.Name, short(live), short(adopt.SpecFingerprint))
		r.Log.Warn("adopted workload drifted; refusing to reconcile",
			"namespace", adopt.Namespace, "name", adopt.Name, "live", live, "adopted", adopt.SpecFingerprint)
	default:
		res.Status.ObservedRevision = res.Spec.Revision
		res.Status.Phase = "Adopted"
		condition.Status = metav1.ConditionTrue
		condition.Reason = "MatchesAdoptedFingerprint"
		condition.Message = "running as adopted; no cluster work performed"
		r.reportAdopted(ctx, res, adopt)
	}
	apimeta.SetStatusCondition(&res.Status.Conditions, condition)
	if err := r.Client.Status().Patch(ctx, res, patch); err != nil {
		return ctrl.Result{}, err
	}

	// Requeued on the same floor a managed deployment gets, and for a stronger reason. Neither
	// watch below can ever wake an adopted object: `declaredFor` maps back from the owner labels
	// this operator's render stamps, and an adopted object was put there by something else and
	// carries none — nor is it the rendered kind being watched. So without a requeue the
	// fingerprint check, which is the whole reason this path exists, runs once at startup and
	// then never again, and a workload that drifts afterwards is never noticed.
	return ctrl.Result{RequeueAfter: resyncAfterApply}, nil
}

// reportAdopted tells the plane what an adopted deployment is actually running.
//
// The adopted path applies nothing, which was taken to mean it had nothing to say — so it said
// nothing, and a deployment the plane had enrolled and that was serving normally reported no
// replicas and no counts for as long as it lived. Not applying is not the same as not observing:
// the pods are there to be read, and they are the only thing that makes an adopted deployment
// distinguishable from one that never came up.
//
// Only on the branch where the fingerprint matches. A drifted object is one this operator has
// refused, and reporting counts off it would put `Serving` on the plane's copy of a deployment
// whose refusal is the fact that actually matters.
//
// A failure to read or send is swallowed for the same reason the rendered path swallows it: this
// is a progress report, the reconcile itself succeeded, and the next pass restates it.
func (r *SyncedResourceReconciler) reportAdopted(ctx context.Context, res *v1alpha1.SyncedResource, adopt *v1alpha1.AdoptRef) {
	if r.reporter() == nil || res.Spec.Kind != "deployment" {
		return
	}

	var dep wire.DeploymentDesired
	if err := decodePayload(res, &dep); err != nil {
		r.Log.Warn("could not decode an adopted deployment to report it", "resource", res.Name, "error", err)
		return
	}
	if dep.DeploymentID == "" {
		return
	}

	out, err := r.observeAdoptedRollout(ctx, adopt, dep)
	if err != nil {
		r.Log.Warn("could not read what an adopted deployment is running",
			"deployment", dep.DeploymentID, "namespace", adopt.Namespace, "name", adopt.Name, "error", err)
		return
	}
	r.reportRollout(res, dep, out)
}

// liveFingerprint digests the pod template of the object named by an adoption, as it is right now.
func (r *SyncedResourceReconciler) liveFingerprint(ctx context.Context, adopt *v1alpha1.AdoptRef) (string, error) {
	key := types.NamespacedName{Namespace: adopt.Namespace, Name: adopt.Name}
	switch strings.ToLower(adopt.Kind) {
	case "deployment":
		var d appsv1.Deployment
		if err := r.Client.Get(ctx, key, &d); err != nil {
			return "", err
		}
		return workload.Fingerprint(&d.Spec.Template.Spec), nil
	case "statefulset":
		var s appsv1.StatefulSet
		if err := r.Client.Get(ctx, key, &s); err != nil {
			return "", err
		}
		return workload.Fingerprint(&s.Spec.Template.Spec), nil
	case "pod":
		var p corev1.Pod
		if err := r.Client.Get(ctx, key, &p); err != nil {
			return "", err
		}
		return workload.FingerprintPod(&p), nil
	default:
		return "", fmt.Errorf("cannot verify an adopted %s", adopt.Kind)
	}
}

// short renders a digest at the length a human compares by eye.
func short(fingerprint string) string {
	trimmed := strings.TrimPrefix(fingerprint, "sha256:")
	if trimmed == "" {
		return "nothing"
	}
	if len(trimmed) > 12 {
		return trimmed[:12]
	}
	return trimmed
}
