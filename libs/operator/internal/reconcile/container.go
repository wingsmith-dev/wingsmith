package reconcile

import (
	"context"
	"fmt"
	"strconv"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/wingsmith-dev/wingsmith/libs/operator/api/v1alpha1"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/backend"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/render"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch;create;update;patch;delete

var (
	deploymentGVK = schema.GroupVersionKind{Group: "apps", Version: "v1", Kind: "Deployment"}
	serviceGVK    = schema.GroupVersionKind{Version: "v1", Kind: "Service"}
)

// containerRenderer runs a `runtime: container` deployment as an apps/v1 Deployment and a
// Service, with no Dynamo involved (P8.2).
type containerRenderer struct{ *KubeBackend }

// Apply renders the Deployment and Service and server-side applies both, unless the live
// Deployment was already built from these inputs.
func (b containerRenderer) Apply(ctx context.Context, d backend.Desired) (backend.Applied, error) {
	r := b.Reconciler
	dep := d.Deployment
	objs, err := render.Container(render.Inputs{
		Deployment: dep,
		Artifact:   d.Artifact,
		Layout: render.Layout{
			DevicesPerNode:  d.Layout.DevicesPerNode,
			NodesPerReplica: d.Layout.NodesPerReplica,
			Domain:          d.Layout.Domain,
		},
	}, r.Render)
	if err != nil {
		return backend.Applied{}, &backend.Rejected{Reason: "RenderFailed", Message: err.Error()}
	}
	owner := metav1.ObjectMeta{Namespace: d.Owner.Namespace, Name: d.Owner.Name, UID: types.UID(d.Owner.UID)}
	var rendered []*unstructured.Unstructured
	for _, typed := range []runtime.Object{objs.Deployment, objs.Service} {
		m, err := render.Unstructured(typed)
		if err != nil {
			return backend.Applied{}, fmt.Errorf("convert %s: %w", objs.Deployment.Name, err)
		}
		obj := &unstructured.Unstructured{Object: m}
		render.Owned(obj, &owner, v1alpha1.GroupVersion.WithKind("SyncedResource"))
		rendered = append(rendered, obj)
	}
	name := objs.Deployment.Name

	if d.Hold != nil {
		if err := d.Hold(ctx); err != nil {
			return backend.Applied{}, err
		}
	}

	current, err := b.current(ctx, dep, name, objs.Deployment.Annotations[render.LayoutAnnotation])
	if err != nil {
		return backend.Applied{}, err
	}
	if current {
		return backend.Applied{Name: name, Unchanged: true}, nil
	}
	for _, obj := range rendered {
		if err := r.Client.Patch(ctx, obj, client.Apply, fieldOwner, client.ForceOwnership); err != nil {
			if clusterRejected(err) {
				return backend.Applied{}, &backend.Rejected{Reason: "ClusterRejected", Message: fmt.Sprintf(
					"the cluster refused %s %s and will refuse it again unchanged: %s",
					obj.GetKind(), obj.GetName(), err.Error())}
			}
			return backend.Applied{}, fmt.Errorf("apply %s %s: %w", obj.GetKind(), obj.GetName(), err)
		}
	}
	r.Log.Info("applied", "deployment", dep.DeploymentID, "generation", dep.Generation,
		"replicas", dep.MinReplicas, "namespace", r.Render.Namespace, "runtime", wire.RuntimeContainer)
	return backend.Applied{Name: name}, nil
}

// current says the live Deployment and Service exist and the Deployment was built from this
// generation, digest and layout, so nothing is written.
func (b containerRenderer) current(ctx context.Context, dep wire.DeploymentDesired, name, layout string) (bool, error) {
	r := b.Reconciler
	key := types.NamespacedName{Namespace: r.Render.Namespace, Name: name}
	live, err := b.get(ctx, deploymentGVK, key)
	if err != nil || live == nil {
		return false, err
	}
	got := live.GetAnnotations()
	if live.GetLabels()[deploymentIDLabel] != dep.DeploymentID ||
		got[render.GenerationAnnotation] != strconv.FormatInt(dep.Generation, 10) ||
		got[render.ServingDigestAnnotation] != dep.Serving.Digest ||
		got[render.LayoutAnnotation] != layout {
		return false, nil
	}
	svc, err := b.get(ctx, serviceGVK, key)
	return svc != nil, err
}

func (b containerRenderer) get(ctx context.Context, gvk schema.GroupVersionKind, key types.NamespacedName) (*unstructured.Unstructured, error) {
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(gvk)
	switch err := b.Reconciler.Client.Get(ctx, key, obj); {
	case apierrors.IsNotFound(err):
		return nil, nil
	case err != nil:
		return nil, fmt.Errorf("read %s %s: %w", gvk.Kind, key.Name, err)
	}
	return obj, nil
}

// Prune deletes what this deployment rendered under any other name, after a rename.
func (b containerRenderer) Prune(ctx context.Context, dep wire.DeploymentDesired, current string) error {
	return b.remove(ctx, dep.DeploymentID, current)
}

// Delete removes every Deployment and Service rendered for the id.
func (b containerRenderer) Delete(ctx context.Context, deploymentID string) error {
	return b.remove(ctx, deploymentID, "")
}

func (b containerRenderer) remove(ctx context.Context, id, keep string) error {
	r := b.Reconciler
	for _, gvk := range []schema.GroupVersionKind{deploymentGVK, serviceGVK} {
		list := &unstructured.UnstructuredList{}
		list.SetGroupVersionKind(gvk.GroupVersion().WithKind(gvk.Kind + "List"))
		err := r.Client.List(ctx, list,
			client.InNamespace(r.Render.Namespace),
			client.MatchingLabels{deploymentIDLabel: id, render.RuntimeLabel: wire.RuntimeContainer},
		)
		if err != nil {
			return fmt.Errorf("list %s for %s: %w", gvk.Kind, id, err)
		}
		for i := range list.Items {
			obj := &list.Items[i]
			if obj.GetName() == keep {
				continue
			}
			switch err := r.Client.Delete(ctx, obj); {
			case err == nil:
				r.Log.Info("removed rendered container object", "deployment", id,
					"kind", gvk.Kind, "name", obj.GetName(), "namespace", obj.GetNamespace())
			case apierrors.IsNotFound(err):
			default:
				return fmt.Errorf("delete %s %s: %w", gvk.Kind, obj.GetName(), err)
			}
		}
	}
	return nil
}

// Observe reads the worker pods. Desired is what was rendered; the pods say which replica is
// where, which the Deployment's status counts cannot.
func (b containerRenderer) Observe(ctx context.Context, dep wire.DeploymentDesired) (backend.Rollout, error) {
	r := b.Reconciler
	var pods corev1.PodList
	err := r.Client.List(ctx, &pods,
		client.InNamespace(r.Render.Namespace),
		client.MatchingLabels{deploymentIDLabel: dep.DeploymentID, render.RuntimeLabel: wire.RuntimeContainer},
	)
	if err != nil {
		return backend.Rollout{}, fmt.Errorf("list container pods for %s: %w", dep.DeploymentID, err)
	}
	image := ""
	if dep.Serving.Container != nil {
		image = dep.Serving.Container.Image
	}
	out := r.rolloutFrom(pods.Items, dep, render.Replicas(dep), wire.RuntimeContainer, image)
	if out.desired > 0 && out.ready < out.desired {
		out.trouble = troubleFromPods(pods.Items)
	}
	return out.status(time.Now().UTC()), nil
}
