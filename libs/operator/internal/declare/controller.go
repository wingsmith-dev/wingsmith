package declare

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"golang.org/x/time/rate"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/workqueue"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	"sigs.k8s.io/controller-runtime/pkg/source"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// +kubebuilder:rbac:groups=plane.wingsmith.dev,resources=workloads,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=plane.wingsmith.dev,resources=workloads/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=plane.wingsmith.dev,resources=workloads/finalizers,verbs=update
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch

// The resource this package syncs, and the names it writes.
var WorkloadGVK = schema.GroupVersionKind{Group: "plane.wingsmith.dev", Version: "v1alpha1", Kind: "Workload"}

const (
	APIVersion     = "plane.wingsmith.dev/v1alpha1"
	ProjectLabel   = "plane.wingsmith.dev/project"
	Finalizer      = "plane.wingsmith.dev/sync"
	ConditionSync  = "Synced"
	ConditionReady = "Ready"
	// Stalled and Reconciling are the kstatus abnormal-true conditions: Argo CD, Flux and
	// `kubectl wait` read Stalled=True as failed and Reconciling=True as in progress.
	ConditionStalled     = "Stalled"
	ConditionReconciling = "Reconciling"

	ReasonApplied     = "Applied"
	ReasonConflict    = "ManagerConflict"
	ReasonObserved    = "Observed"
	ReasonInvalid     = "Invalid"
	ReasonForbidden   = "Forbidden"
	ReasonUnreachable = "PlaneUnreachable"
	ReasonPlaneError  = "PlaneError"
	ReasonDeleting    = "Deleting"
	ReasonAwaiting    = "AwaitingStatus"
	ReasonDeleteStuck = "DeleteFailing"
	// ReasonNamespaceNotAllowed: the project link does not list this namespace.
	ReasonNamespaceNotAllowed = "NamespaceNotAllowed"

	// DeleteStallAfter is how many failed plane deletes leave the resource Stalled. It keeps
	// retrying (at the backoff cap); patching spec.deletionPolicy to Orphan releases it.
	DeleteStallAfter = 10

	// DeletionOrphan in spec.deletionPolicy leaves the plane object when the resource goes.
	DeletionOrphan = "Orphan"
)

// Reconciler applies each Workload resource in a project namespace to the plane.
type Reconciler struct {
	Client client.Client
	Plane  Plane
	// Cluster is this cluster's id as the plane knows it: the default allowed cluster.
	Cluster string
	// DefaultProject is used for a namespace without the project label; empty means such a
	// namespace is not synced at all.
	DefaultProject string
	Log            *slog.Logger
	// Retry is how soon an unreachable or failing plane is tried again; Resync how often a
	// synced resource is re-applied as a drift check.
	Retry, Resync time.Duration
	// QPS and Burst feed the work queue's bucket limiter and Concurrency caps parallel
	// reconciles, so a reconnect relisting every resource does not flood the plane.
	QPS, Burst, Concurrency int
}

func newWorkload() *unstructured.Unstructured {
	u := &unstructured.Unstructured{}
	u.SetGroupVersionKind(WorkloadGVK)
	return u
}

// Reconcile implements reconcile.Reconciler.
func (r *Reconciler) Reconcile(ctx context.Context, req reconcile.Request) (reconcile.Result, error) {
	obj := newWorkload()
	if err := r.Client.Get(ctx, req.NamespacedName, obj); err != nil {
		return reconcile.Result{}, client.IgnoreNotFound(err)
	}
	project, err := r.project(ctx, obj.GetNamespace())
	if err != nil {
		return reconcile.Result{}, err
	}
	if obj.GetDeletionTimestamp() != nil {
		return r.finalize(ctx, obj, project)
	}
	if project == "" {
		// Not a project namespace (any more): nothing to sync, and nothing to hold back.
		return reconcile.Result{}, r.release(ctx, obj)
	}
	before := obj.DeepCopy()
	if controllerutil.AddFinalizer(obj, Finalizer) {
		if err := r.Client.Patch(ctx, obj, client.MergeFrom(before)); err != nil {
			return reconcile.Result{}, err
		}
	}
	result, err := r.Plane.Apply(ctx, &wire.ManifestApply{
		Kind:       "Workload",
		Project:    project,
		Name:       obj.GetName(),
		Namespace:  obj.GetNamespace(),
		UID:        string(obj.GetUID()),
		Body:       r.body(obj, project),
		Generation: obj.GetGeneration(),
	})
	if err != nil {
		if errors.Is(err, context.Canceled) {
			return reconcile.Result{}, err
		}
		return r.unreachable(ctx, obj, err)
	}
	return r.report(ctx, obj, result)
}

func (r *Reconciler) project(ctx context.Context, namespace string) (string, error) {
	ns := &corev1.Namespace{}
	if err := r.Client.Get(ctx, types.NamespacedName{Name: namespace}, ns); err != nil {
		if apierrors.IsNotFound(err) {
			return r.DefaultProject, nil
		}
		return "", err
	}
	if p := ns.Labels[ProjectLabel]; p != "" {
		return p, nil
	}
	return r.DefaultProject, nil
}

// body is the manifest the plane is sent. The resource's own spec is never written: the
// this-cluster-only default lives here and in status, so GitOps tools see no drift.
func (r *Reconciler) body(obj *unstructured.Unstructured, project string) map[string]any {
	spec, _, _ := unstructured.NestedMap(obj.Object, "spec")
	if spec == nil {
		spec = map[string]any{}
	}
	// The resource's own lifecycle, not part of the plane manifest.
	delete(spec, "deletionPolicy")
	_, pinned := spec["cluster"]
	_, clusters, _ := unstructured.NestedFieldNoCopy(spec, "placement", "allowedClusters")
	_, regions, _ := unstructured.NestedFieldNoCopy(spec, "placement", "allowedRegions")
	if !pinned && !clusters && !regions {
		_ = unstructured.SetNestedStringSlice(spec, []string{r.Cluster}, "placement", "allowedClusters")
	}
	meta := map[string]any{"name": obj.GetName(), "namespace": project}
	if labels := obj.GetLabels(); len(labels) > 0 {
		meta["labels"] = toAny(labels)
	}
	annotations := map[string]string{}
	for k, v := range obj.GetAnnotations() {
		// Client bookkeeping, not part of the declaration.
		if strings.HasPrefix(k, "kubectl.kubernetes.io/") || strings.HasPrefix(k, "argocd.argoproj.io/") {
			continue
		}
		annotations[k] = v
	}
	if len(annotations) > 0 {
		meta["annotations"] = toAny(annotations)
	}
	return map[string]any{"apiVersion": APIVersion, "kind": "Workload", "metadata": meta, "spec": spec}
}

func toAny(m map[string]string) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func (r *Reconciler) finalize(ctx context.Context, obj *unstructured.Unstructured, project string) (reconcile.Result, error) {
	if !controllerutil.ContainsFinalizer(obj, Finalizer) {
		return reconcile.Result{}, nil
	}
	if policy, _, _ := unstructured.NestedString(obj.Object, "spec", "deletionPolicy"); project == "" || policy == DeletionOrphan {
		return reconcile.Result{}, r.release(ctx, obj)
	}
	result, err := r.Plane.Delete(ctx, &wire.ManifestDelete{
		Kind: "Workload", Project: project, Name: obj.GetName(), Namespace: obj.GetNamespace(), UID: string(obj.GetUID()),
	})
	if err != nil {
		if errors.Is(err, context.Canceled) {
			return reconcile.Result{}, err
		}
		return r.unreachable(ctx, obj, err)
	}
	switch result.Result {
	case "deleted", "released", "absent", "forbidden", "invalid":
		// forbidden: the project was unlinked from this cluster, so the plane object is no longer
		// this cluster's to delete. invalid: the plane does not know the object (gone, or a kind
		// or name it never accepted), so there is nothing left to delete. Holding the resource
		// Terminating would fix neither.
		r.Log.Info("workload released", "namespace", obj.GetNamespace(), "name", obj.GetName(), "result", result.Result)
		return reconcile.Result{}, r.release(ctx, obj)
	}
	// conflict or a plane error: retry with exponential backoff, and after DeleteStallAfter
	// attempts say so with Stalled=True. Setting spec.deletionPolicy: Orphan unsticks it.
	attempts, _, _ := unstructured.NestedInt64(obj.Object, "status", "deleteAttempts")
	attempts++
	msg := result.Error
	if msg == "" {
		msg = "plane answered " + result.Result
	}
	return r.write(ctx, obj, func(st *status) {
		st.obj["deleteAttempts"] = attempts
		st.set(ConditionSync, metav1.ConditionFalse, ReasonDeleting, msg)
		if attempts >= DeleteStallAfter {
			st.set(ConditionStalled, metav1.ConditionTrue, ReasonDeleteStuck, fmt.Sprintf(
				"plane delete failed %d times: %s; set spec.deletionPolicy: Orphan to release the resource and keep the plane object", attempts, msg))
			st.drop(ConditionReconciling)
		}
	}, r.deleteBackoff(attempts))
}

// deleteBackoff doubles Retry per failed attempt, capped at 32x.
func (r *Reconciler) deleteBackoff(attempts int64) time.Duration {
	shift := attempts - 1
	if shift > 5 {
		shift = 5
	}
	return r.Retry << shift
}

func (r *Reconciler) release(ctx context.Context, obj *unstructured.Unstructured) error {
	before := obj.DeepCopy()
	if controllerutil.RemoveFinalizer(obj, Finalizer) {
		return r.Client.Patch(ctx, obj, client.MergeFrom(before))
	}
	return nil
}

func (r *Reconciler) unreachable(ctx context.Context, obj *unstructured.Unstructured, cause error) (reconcile.Result, error) {
	return r.write(ctx, obj, func(st *status) {
		st.set(ConditionSync, metav1.ConditionUnknown, ReasonUnreachable, cause.Error())
		st.set(ConditionReconciling, metav1.ConditionTrue, ReasonUnreachable, "waiting for the plane")
	}, r.Retry)
}

func (r *Reconciler) report(ctx context.Context, obj *unstructured.Unstructured, res *wire.ManifestResult) (reconcile.Result, error) {
	retry := r.Resync
	result, err := r.write(ctx, obj, func(st *status) {
		st.observe(obj.GetGeneration())
		switch res.Result {
		case "created", "configured", "unchanged":
			st.obj["planeUID"] = res.UID
			st.obj["planeResourceVersion"] = res.RV
			if res.PlaneGeneration != nil {
				st.obj["planeGeneration"] = *res.PlaneGeneration
			}
			st.obj["fieldManager"] = res.Manager
			st.obj["placement"] = map[string]any{"allowedClusters": toAnySlice(res.AllowedClusters)}
			st.set(ConditionSync, metav1.ConditionTrue, ReasonApplied, "applied to the plane: "+res.Result)
			// Ready means serving, which the plane does not report back yet (P1.16).
			st.set(ConditionReady, metav1.ConditionUnknown, ReasonAwaiting, "serving status is not reported yet")
			st.set(ConditionReconciling, metav1.ConditionTrue, ReasonAwaiting, "waiting for serving status")
			st.drop(ConditionStalled)
		case "conflict":
			st.set(ConditionSync, metav1.ConditionFalse, ReasonConflict, res.Error)
			st.stalled(ReasonConflict, res.Error)
		case "invalid", "forbidden":
			reason := ReasonInvalid
			if res.Result == "forbidden" {
				reason = ReasonForbidden
				if res.Reason == ReasonNamespaceNotAllowed {
					reason = ReasonNamespaceNotAllowed
				}
			} else if strings.Contains(res.Error, "observed") {
				reason = ReasonObserved
			}
			st.set(ConditionSync, metav1.ConditionFalse, reason, res.Error)
			st.stalled(reason, res.Error)
		default:
			retry = r.Retry
			st.set(ConditionSync, metav1.ConditionFalse, ReasonPlaneError, res.Error)
			st.set(ConditionReconciling, metav1.ConditionTrue, ReasonPlaneError, res.Error)
		}
	}, retry)
	return result, err
}

func toAnySlice(in []string) []any {
	out := make([]any, len(in))
	for i, v := range in {
		out[i] = v
	}
	return out
}

// status edits .status of one resource. Only status and finalizers are ever written.
type status struct {
	obj        map[string]any
	conditions []metav1.Condition
	generation int64
}

func (s *status) observe(generation int64) { s.obj["observedGeneration"] = generation }

func (s *status) set(t string, st metav1.ConditionStatus, reason, msg string) {
	meta.SetStatusCondition(&s.conditions, metav1.Condition{
		Type: t, Status: st, Reason: reason, Message: msg, ObservedGeneration: s.generation,
	})
}

func (s *status) drop(types ...string) {
	for _, t := range types {
		meta.RemoveStatusCondition(&s.conditions, t)
	}
}

func (s *status) stalled(reason, msg string) {
	s.set(ConditionReady, metav1.ConditionFalse, reason, msg)
	s.set(ConditionStalled, metav1.ConditionTrue, reason, msg)
	s.drop(ConditionReconciling)
}

func (r *Reconciler) write(ctx context.Context, obj *unstructured.Unstructured, edit func(*status), after time.Duration) (reconcile.Result, error) {
	before := obj.DeepCopy()
	raw, _, _ := unstructured.NestedMap(obj.Object, "status")
	if raw == nil {
		raw = map[string]any{}
	}
	st := &status{obj: raw, generation: obj.GetGeneration()}
	if list, ok := raw["conditions"].([]any); ok {
		for _, item := range list {
			m, ok := item.(map[string]any)
			if !ok {
				continue
			}
			var c metav1.Condition
			if err := runtime.DefaultUnstructuredConverter.FromUnstructured(m, &c); err == nil {
				st.conditions = append(st.conditions, c)
			}
		}
	}
	edit(st)
	conds := make([]any, 0, len(st.conditions))
	for i := range st.conditions {
		m, err := runtime.DefaultUnstructuredConverter.ToUnstructured(&st.conditions[i])
		if err != nil {
			return reconcile.Result{}, err
		}
		conds = append(conds, m)
	}
	raw["conditions"] = conds
	if err := unstructured.SetNestedField(obj.Object, raw, "status"); err != nil {
		return reconcile.Result{}, err
	}
	if err := r.Client.Status().Patch(ctx, obj, client.MergeFrom(before)); err != nil {
		if apierrors.IsNotFound(err) {
			return reconcile.Result{}, nil
		}
		return reconcile.Result{}, err
	}
	return reconcile.Result{RequeueAfter: after}, nil
}

// SetupWithManager registers the controller, a Namespace watch (a label change moves every
// resource in it), and a relist of every resource whenever the hub session reconnects.
func (r *Reconciler) SetupWithManager(mgr manager.Manager) error {
	if r.Retry == 0 {
		r.Retry = 30 * time.Second
	}
	if r.Resync == 0 {
		r.Resync = 10 * time.Minute
	}
	reconnects := make(chan event.TypedGenericEvent[*unstructured.Unstructured])
	if err := mgr.Add(manager.RunnableFunc(func(ctx context.Context) error {
		return r.watchReconnects(ctx, reconnects)
	})); err != nil {
		return err
	}
	return ctrl.NewControllerManagedBy(mgr).
		Named("declare-workload").
		WithOptions(r.controllerOptions()).
		For(newWorkload()).
		Watches(&corev1.Namespace{}, handler.EnqueueRequestsFromMapFunc(r.inNamespace)).
		WatchesRawSource(source.Channel(reconnects, &handler.TypedEnqueueRequestForObject[*unstructured.Unstructured]{})).
		Complete(r)
}

// controllerOptions is the per-item exponential backoff combined with a shared token bucket,
// and a small worker pool.
func (r *Reconciler) controllerOptions() controller.Options {
	qps, burst, workers := r.QPS, r.Burst, r.Concurrency
	if qps <= 0 {
		qps = 10
	}
	if burst <= 0 {
		burst = 50
	}
	if workers <= 0 {
		workers = 4
	}
	return controller.Options{
		MaxConcurrentReconciles: workers,
		RateLimiter: workqueue.NewTypedMaxOfRateLimiter(
			workqueue.NewTypedItemExponentialFailureRateLimiter[reconcile.Request](5*time.Millisecond, 5*time.Minute),
			&workqueue.TypedBucketRateLimiter[reconcile.Request]{Limiter: rate.NewLimiter(rate.Limit(qps), burst)},
		),
	}
}

func (r *Reconciler) inNamespace(ctx context.Context, o client.Object) []reconcile.Request {
	return r.requests(ctx, client.InNamespace(o.GetName()))
}

func (r *Reconciler) requests(ctx context.Context, opts ...client.ListOption) []reconcile.Request {
	list := &unstructured.UnstructuredList{}
	list.SetGroupVersionKind(WorkloadGVK.GroupVersion().WithKind("WorkloadList"))
	if err := r.Client.List(ctx, list, opts...); err != nil {
		r.Log.Warn("list workloads", "error", err)
		return nil
	}
	out := make([]reconcile.Request, 0, len(list.Items))
	for i := range list.Items {
		out = append(out, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&list.Items[i])})
	}
	return out
}

// Relist reconciles every Workload resource: the reapply after a reconnect. Replays are no-ops on
// the plane, which answers unchanged.
func (r *Reconciler) Relist(ctx context.Context) []reconcile.Request { return r.requests(ctx) }

func (r *Reconciler) watchReconnects(ctx context.Context, out chan<- event.TypedGenericEvent[*unstructured.Unstructured]) error {
	seen := r.Plane.Connects()
	tick := time.NewTicker(2 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-tick.C:
		}
		now := r.Plane.Connects()
		if now == seen {
			continue
		}
		seen = now
		for _, req := range r.Relist(ctx) {
			u := newWorkload()
			u.SetNamespace(req.Namespace)
			u.SetName(req.Name)
			select {
			case out <- event.TypedGenericEvent[*unstructured.Unstructured]{Object: u}:
			case <-ctx.Done():
				return nil
			}
		}
	}
}

// String names the reconciler in logs.
func (r *Reconciler) String() string { return fmt.Sprintf("declare(cluster=%s)", r.Cluster) }
