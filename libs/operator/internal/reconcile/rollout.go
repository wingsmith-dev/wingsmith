package reconcile

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/observe"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// graphDeploymentLabel is how the Dynamo operator marks the pods it created for one graph.
//
// Its label, not ours. The labels this operator renders sit on the graph deployment, and Dynamo
// does not copy them onto the pod templates it derives — so the only reliable link from a running
// pod back to the deployment that asked for it is the one Dynamo itself writes, keyed by the
// graph's name. That name is `render.WorkloadName(dep)`, which is the deployment's own name.
const graphDeploymentLabel = "nvidia.com/dynamo-graph-deployment-name"

// rollout is what is actually running for one deployment, as opposed to what was applied.
//
// The distinction is the entire point. `reconcileDeployment` concludes "applied" and that is a
// statement about a write to the API server; it says nothing about whether a single worker ever
// started. Until this existed the plane had no other source, so a deployment that was serving
// happily read `placed · 0/0 ready` forever — which is indistinguishable, to somebody looking at
// the console, from one that never came up at all.
type rollout struct {
	desired     int
	ready       int
	updated     int
	unavailable int
	replicas    []wire.ReplicaObserved
}

// phase is the observed phase these counts amount to.
//
// `placed` rather than `pending` when nothing is ready yet: the work has been handed to the
// cluster and accepted, and pending is reserved for the orderings that stop it being handed over
// at all. `degraded` is any shortfall while something is still serving, which is the state a
// rolling update passes through and the one a crash-looping replica sits in.
func (r rollout) phase() string {
	switch {
	case r.desired == 0:
		return phasePlaced
	case r.ready == 0:
		return phasePlaced
	case r.ready < r.desired:
		return phaseDegraded
	default:
		return phaseRunning
	}
}

// reason is the closed-set name for what these counts amount to (MESSAGES.md).
//
// Derived here rather than carried over from the apply, because "applied" answers a different
// question and answering it again once replicas are up is how the plane came to describe a serving
// deployment as one still waiting for replicas.
func (r rollout) reason() string {
	switch r.phase() {
	case phaseRunning:
		return "Serving"
	case phaseDegraded:
		return "Progressing"
	default:
		return "Applied"
	}
}

func (r rollout) message() string {
	return fmt.Sprintf("%d of %d replicas ready", r.ready, r.desired)
}

// observeRollout reads what is running for one deployment.
//
// Pods rather than the intermediate Deployment object, because a pod is the thing that is either
// serving or not and is also the unit the plane models as a replica — reading the Deployment
// would give counts with no way to say which replica is on which node, and the allocation view
// needs the latter.
func (r *SyncedResourceReconciler) observeRollout(
	ctx context.Context,
	dep wire.DeploymentDesired,
	recipe wire.RecipeDesired,
) (rollout, error) {
	var pods corev1.PodList
	err := r.Client.List(ctx, &pods,
		client.InNamespace(r.Render.Namespace),
		client.MatchingLabels{graphDeploymentLabel: render.WorkloadName(dep)},
	)
	if err != nil {
		return rollout{}, fmt.Errorf("list pods for %s: %w", dep.DeploymentID, err)
	}

	out := rollout{desired: render.Replicas(dep)}
	now := time.Now().UTC().Format(time.RFC3339Nano)

	for i := range pods.Items {
		pod := &pods.Items[i]
		if !pod.DeletionTimestamp.IsZero() {
			// Terminating. Counting it as unavailable would make a completed rollout look
			// degraded for as long as the old pod takes to go away.
			continue
		}

		ready := podReady(pod)
		if ready {
			out.ready++
		} else {
			out.unavailable++
		}
		if pod.Labels["pod-template-hash"] != "" {
			out.updated++
		}

		out.replicas = append(out.replicas, wire.ReplicaObserved{
			ReplicaID:    observe.ReplicaID(r.ClusterID, pod.Namespace, pod.Name),
			Name:         pod.Name,
			DeploymentID: dep.DeploymentID,
			ClusterID:    r.ClusterID,
			NodeID:       nodeIDFor(r.ClusterID, pod.Spec.NodeName),
			// Empty, never nil. The field is required on the wire and a nil slice marshals to
			// `null`, which the plane refuses — so every replica frame this loop sent was
			// rejected as invalid and the replica rollup stayed at zero for deployments that
			// were plainly serving. Empty is also the truthful value: which devices a replica
			// holds is an Allocation, reported by the observe loop, and a pod spec carries a
			// count rather than the ids.
			DeviceIDs:          []string{},
			Phase:              replicaPhase(pod, ready),
			Ready:              ready,
			Engine:             recipe.Engine,
			EngineVersion:      recipe.EngineVersion,
			RecipeID:           dep.RecipeID,
			RestartCount:       restarts(pod),
			ObservedGeneration: dep.Generation,
			StartedAt:          startedAt(pod),
			ObservedAt:         now,
		})
	}
	return out, nil
}

// nodeIDFor is the observed id of the node a replica landed on, or empty while it has none.
//
// An unscheduled pod has no node name, and minting an id from the empty string would produce a
// stable, meaningless id that the plane would then try to resolve against its inventory.
func nodeIDFor(clusterID, nodeName string) string {
	if nodeName == "" {
		return ""
	}
	return observe.NodeID(clusterID, nodeName)
}

func podReady(pod *corev1.Pod) bool {
	if pod.Status.Phase != corev1.PodRunning {
		return false
	}
	for _, condition := range pod.Status.Conditions {
		if condition.Type == corev1.PodReady {
			return condition.Status == corev1.ConditionTrue
		}
	}
	return false
}

// replicaPhase maps a pod onto the phase enum the contract defines for a replica.
//
// A pod that is Running but not Ready is `starting`, not `running`. For an inference worker that
// distinction is most of the wait: the container is up for as long as it takes to load tens of
// gigabytes of weights into VRAM, and reporting it as running would tell the plane a replica is
// serving when nothing can reach it yet.
func replicaPhase(pod *corev1.Pod, ready bool) string {
	switch pod.Status.Phase {
	case corev1.PodPending:
		return "pending"
	case corev1.PodSucceeded:
		return "succeeded"
	case corev1.PodFailed:
		return "failed"
	case corev1.PodRunning:
		if ready {
			return "running"
		}
		return "starting"
	default:
		return "pending"
	}
}

func restarts(pod *corev1.Pod) int {
	total := 0
	for _, status := range pod.Status.ContainerStatuses {
		total += int(status.RestartCount)
	}
	return total
}

func startedAt(pod *corev1.Pod) *string {
	if pod.Status.StartTime == nil {
		return nil
	}
	at := pod.Status.StartTime.UTC().Format(time.RFC3339Nano)
	return &at
}
