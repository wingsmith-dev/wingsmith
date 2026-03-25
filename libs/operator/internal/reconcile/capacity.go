package reconcile

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// acceleratorResource is the extended resource the device plugin advertises.
const acceleratorResource = corev1.ResourceName("nvidia.com/gpu")

// schedulable reports whether any single node could actually run one replica.
//
// This is the precondition that fails silently otherwise. A graph deployment asking for
// accelerators the cluster does not advertise is accepted by the API server and then sits in
// Pending forever, and the reason a human eventually finds is "0/12 nodes are available", which
// says nothing about why. Checking first turns that into a stated cause on the declared object.
//
// It also covers the case nobody thinks to check: the device plugin is not installed. A node with
// eight idle accelerators advertises none of them until something tells Kubernetes they exist, and
// from the scheduler's side that is indistinguishable from having no accelerators at all.
//
// One node, not the cluster total. A replica's devices are consumed by one pod, and a
// tensor-parallel replica cannot be split across machines at all, so capacity spread thinly over
// many nodes does not satisfy a request for several devices.
func schedulable(ctx context.Context, c client.Client, dep wire.DeploymentDesired, selector map[string]string) (bool, int64, error) {
	want := int64(dep.Placement.DevicesPerReplica)
	if want < 1 {
		want = 1
	}

	var nodes corev1.NodeList
	if err := c.List(ctx, &nodes); err != nil {
		return false, 0, fmt.Errorf("list nodes: %w", err)
	}

	var best int64
	for i := range nodes.Items {
		node := &nodes.Items[i]
		if !nodeUsable(node) || !matchesSelector(node.Labels, selector) {
			continue
		}
		if available := node.Status.Allocatable.Name(acceleratorResource, "").Value(); available > best {
			best = available
		}
	}
	return best >= want, best, nil
}

// nodeUsable reports whether the scheduler would consider placing anything on a node.
//
// Cordoned nodes are excluded. A drain in progress is exactly when this check matters most: the
// accelerators are still allocatable and still reported, and treating them as available would
// promise capacity that is deliberately being taken away.
func nodeUsable(node *corev1.Node) bool {
	if node.Spec.Unschedulable {
		return false
	}
	for _, condition := range node.Status.Conditions {
		if condition.Type == corev1.NodeReady {
			return condition.Status == corev1.ConditionTrue
		}
	}
	return false
}

func matchesSelector(labels, selector map[string]string) bool {
	for key, want := range selector {
		if labels[key] != want {
			return false
		}
	}
	return true
}
