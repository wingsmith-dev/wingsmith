package declare

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
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
	ConditionClash = "Conflict"
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
	if controllerutil.AddFinalizer(obj, Finalizer) {
		if err := r.Client.Update(ctx, obj); err != nil {
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
	if project == "" {
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
	case "deleted", "released", "absent", "forbidden":
		// forbidden: the project was unlinked from this cluster, so the plane object is no longer
		// this cluster's to delete. Holding the resource Terminating would fix nothing.
		r.Log.Info("workload released", "namespace", obj.GetNamespace(), "name", obj.GetName(), "result", result.Result)
		return reconcile.Result{}, r.release(ctx, obj)
	}
	return r.write(ctx, obj, func(st *status) {
		st.set(ConditionSync, metav1.ConditionFalse, ReasonDeleting, result.Error)
	}, r.Retry)
}

func (r *Reconciler) release(ctx context.Context, obj *unstructured.Unstructured) error {
	if controllerutil.RemoveFinalizer(obj, Finalizer) {
		return r.Client.Update(ctx, obj)
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
			st.set(ConditionClash, metav1.ConditionFalse, ReasonApplied, "")
			st.set(ConditionReady, metav1.ConditionTrue, ReasonApplied, "the plane holds this generation")
			st.drop(ConditionStalled, ConditionReconciling)
		case "conflict":
			st.set(ConditionSync, metav1.ConditionFalse, ReasonConflict, res.Error)
			st.set(ConditionClash, metav1.ConditionTrue, ReasonConflict, res.Error)
			st.stalled(ReasonConflict, res.Error)
		case "invalid", "forbidden":
			reason := ReasonInvalid
			if res.Result == "forbidden" {
				reason = ReasonForbidden
			} else if strings.Contains(res.Error, "observed") {
				reason = ReasonObserved
			}
			st.set(ConditionSync, metav1.ConditionFalse, reason, res.Error)
			st.set(ConditionClash, metav1.ConditionFalse, reason, "")
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
	if err := r.Client.Status().Update(ctx, obj); err != nil {
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
		For(newWorkload()).
		Watches(&corev1.Namespace{}, handler.EnqueueRequestsFromMapFunc(r.inNamespace)).
		WatchesRawSource(source.Channel(reconnects, &handler.TypedEnqueueRequestForObject[*unstructured.Unstructured]{})).
		Complete(r)
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
