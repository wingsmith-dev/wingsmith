package reconcile

import (
	"context"
	"errors"
	"fmt"
	"time"

	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/wingsmith-dev/wingsmith/libs/operator/api/v1alpha1"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/backend"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/render"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// InventorySource is the supply half of a backend; observe.Observer implements it.
type InventorySource interface {
	Inventory(ctx context.Context) (backend.Inventory, error)
}

// KubeBackend is the Kubernetes implementation of backend.Backend. It drives each deployment
// through the WorkloadRenderer for its runtime: Dynamo, or a plain container (P8.2).
type KubeBackend struct {
	Reconciler *SyncedResourceReconciler
	Supply     InventorySource
}

var _ backend.Backend = (*KubeBackend)(nil)

// WorkloadRenderer is one serving runtime on Kubernetes: what it renders and applies, how it
// collects what earlier applies left behind, how it tears down, and how it reads a rollout back.
type WorkloadRenderer interface {
	Apply(ctx context.Context, d backend.Desired) (backend.Applied, error)
	Prune(ctx context.Context, dep wire.DeploymentDesired, current string) error
	Delete(ctx context.Context, deploymentID string) error
	Observe(ctx context.Context, dep wire.DeploymentDesired) (backend.Rollout, error)
}

// dynamoRenderer renders a DynamoGraphDeployment, server-side applies it, and reads pods back.
type dynamoRenderer struct{ *KubeBackend }

var (
	_ WorkloadRenderer = dynamoRenderer{}
	_ WorkloadRenderer = containerRenderer{}
)

// renderer is the runtime one deployment runs under.
func (b *KubeBackend) renderer(dep wire.DeploymentDesired) WorkloadRenderer {
	if dep.Serving.IsContainer() {
		return containerRenderer{b}
	}
	return dynamoRenderer{b}
}

// Apply implements backend.Backend.
func (b *KubeBackend) Apply(ctx context.Context, d backend.Desired) (backend.Applied, error) {
	return b.renderer(d.Deployment).Apply(ctx, d)
}

// Prune implements backend.Backend.
func (b *KubeBackend) Prune(ctx context.Context, dep wire.DeploymentDesired, current string) error {
	return b.renderer(dep).Prune(ctx, dep, current)
}

// Delete implements backend.Backend. Teardown knows only the id, so every runtime collects what
// it rendered for it; absent is success for each.
func (b *KubeBackend) Delete(ctx context.Context, deploymentID string) error {
	if err := (dynamoRenderer{b}).Delete(ctx, deploymentID); err != nil {
		return err
	}
	return containerRenderer{b}.Delete(ctx, deploymentID)
}

// Observe implements backend.Backend.
func (b *KubeBackend) Observe(ctx context.Context, dep wire.DeploymentDesired) (backend.Rollout, error) {
	return b.renderer(dep).Observe(ctx, dep)
}

// backend is the runtime this reconciler drives.
func (r *SyncedResourceReconciler) backend() *KubeBackend {
	return &KubeBackend{Reconciler: r, Supply: r.Supply}
}

// Apply renders and applies the graph deployment.
//
// Nothing is written when what is running was already built from these inputs. The apply is
// otherwise unconditional, and an unconditional apply makes every change to the renderer a
// fleet-wide reload: Dynamo hashes a component's whole pod template to name the
// DynamoComponentDeployment it creates, so a single byte of difference (even a label nothing
// reads) abandons the running worker and starts a new one. Every model reloads its weights, and on
// a saturated cluster that deadlocks: the new pod cannot schedule until the old one releases the
// accelerators it is still holding.
//
// This is what the serving digest was for. Its definition is *would this produce the same running
// process*, computed by the plane over its own declaration, so a comparison against it is
// insensitive to how this operator happens to render that declaration today.
//
// The cost, stated plainly: while the declaration and the layout stand still, this operator no
// longer corrects drift on the graph deployment, and a fix to the renderer does not reach a running
// workload. Both are deliberate. To force one, bump the deployment's generation, or delete the
// graph deployment as `TopologyImmutable` describes.
func (b dynamoRenderer) Apply(ctx context.Context, d backend.Desired) (backend.Applied, error) {
	r := b.Reconciler
	dep := d.Deployment
	obj, err := render.DynamoGraphDeployment(render.Inputs{
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
	render.Owned(obj, &owner, v1alpha1.GroupVersion.WithKind("SyncedResource"))

	if d.Hold != nil {
		if err := d.Hold(ctx); err != nil {
			return backend.Applied{}, err
		}
	}

	current, err := r.renderIsCurrent(ctx, obj)
	if err != nil {
		return backend.Applied{}, err
	}
	if current {
		r.Log.Info("already built from these inputs; not applying",
			"deployment", dep.DeploymentID, "generation", dep.Generation,
			"digest", dep.Serving.Digest, "namespace", obj.GetNamespace())
		return backend.Applied{Name: obj.GetName(), Unchanged: true}, nil
	}

	if err := r.Client.Patch(ctx, obj, client.Apply, fieldOwner, client.ForceOwnership); err != nil {
		if immutableTopology(err) {
			return backend.Applied{}, &backend.Rejected{Reason: "TopologyImmutable", Message: fmt.Sprintf(
				"%s already exists with a different set of components, and Dynamo refuses to "+
					"change the set on a live graph. Delete the graph deployment to have this "+
					"operator render it again from the current serving configuration: "+
					"kubectl -n %s delete dynamographdeployment %s. The model reloads its "+
					"weights, so this is a deliberate redeploy and not something to do while it "+
					"is serving.", obj.GetName(), obj.GetNamespace(), obj.GetName())}
		}
		if apimeta.IsNoMatchError(err) {
			return backend.Applied{}, fmt.Errorf("apply %s: the cluster does not serve %s; install the Dynamo operator: %w",
				obj.GetName(), render.GVK.Kind, err)
		}
		if clusterRejected(err) {
			return backend.Applied{}, &backend.Rejected{Reason: "ClusterRejected", Message: fmt.Sprintf(
				"the cluster refused %s and will refuse it again unchanged: %s",
				obj.GetName(), err.Error())}
		}
		return backend.Applied{}, fmt.Errorf("apply %s: %w", obj.GetName(), err)
	}
	r.Log.Info("applied", "deployment", dep.DeploymentID, "generation", dep.Generation,
		"replicas", dep.MinReplicas, "namespace", obj.GetNamespace())
	return backend.Applied{Name: obj.GetName()}, nil
}

// Prune collects graph deployments under an old name and component deployments Dynamo abandoned.
func (b dynamoRenderer) Prune(ctx context.Context, dep wire.DeploymentDesired, current string) error {
	r := b.Reconciler
	// A rename renders under a new object name, and the object the old name created is still
	// serving. Nothing else would ever collect it: the declared copy is not being deleted, so
	// teardown never runs, and the apply only ever touches the current name.
	if err := r.pruneRendered(ctx, dep.DeploymentID, current); err != nil {
		return err
	}
	// An edit to the serving block renders the same graph deployment under the same name, and
	// Dynamo answers it by creating a component deployment under a new spec hash and abandoning
	// the previous one, which keeps its accelerators. See supersede.go; on a full node the new
	// pod can never schedule until the abandoned one is gone.
	return r.pruneSuperseded(ctx, current)
}

// Delete removes every graph deployment rendered for the id.
func (b dynamoRenderer) Delete(ctx context.Context, deploymentID string) error {
	return b.Reconciler.pruneRendered(ctx, deploymentID, "")
}

// Observe reads the graph's worker and frontend pods.
func (b dynamoRenderer) Observe(ctx context.Context, dep wire.DeploymentDesired) (backend.Rollout, error) {
	out, err := b.Reconciler.observeRollout(ctx, dep)
	if err != nil {
		return backend.Rollout{}, err
	}
	return out.status(time.Now().UTC()), nil
}

// Inventory implements backend.Backend.
func (b *KubeBackend) Inventory(ctx context.Context) (backend.Inventory, error) {
	if b.Supply == nil {
		return backend.Inventory{}, errors.New("kube backend: no inventory source")
	}
	return b.Supply.Inventory(ctx)
}

// status projects a rollout into the backend vocabulary.
func (r rollout) status(at time.Time) backend.Rollout {
	return backend.Rollout{
		Phase:       r.phase(),
		Reason:      r.reason(),
		Message:     r.message(),
		Desired:     r.desired,
		Ready:       r.ready,
		Updated:     r.updated,
		Unavailable: r.unavailable,
		Replicas:    r.replicas,
		Allocations: r.allocations,
		Roles:       r.roles,
		LastError:   r.trouble.lastError(at.Format(time.RFC3339Nano)),
		Converging:  r.ready < r.desired || (r.frontendUnready && r.trouble == nil),
		ObservedAt:  at,
	}
}
