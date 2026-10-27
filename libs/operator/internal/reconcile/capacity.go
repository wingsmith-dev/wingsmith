package reconcile

import (
	"context"
	"fmt"
	"sort"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// acceleratorResource is the extended resource the device plugin advertises.
const acceleratorResource = corev1.ResourceName("nvidia.com/gpu")

// cliqueLabel names the fabric a node's accelerators belong to. It is the render's constant
// because grouping nodes by one label and selecting on another is not a check.
//
// The same label discovery reads to build an `nvlink` topology domain, and it has to be the same
// one: a domain the plane was told about and a domain the render selects on must be the same set
// of machines, or `single_domain` means one thing in the placement decision and another here.
//
// A node without it is its own domain. That is not a gap to fill in later — it is the only honest
// reading. No label means nothing has asserted a fabric between this machine and any other, and a
// replica that must stay inside one domain therefore cannot leave the node.
const cliqueLabel = render.CliqueLabel

// shape is how one replica's devices are laid out over machines.
//
// Derived, never declared. The plane sends the device count and the serving block sends the
// parallel degrees; how many machines that becomes depends on how big this cluster's machines are, which
// is a local fact and so a local decision — rule 3, the same reason device selection is the
// cluster's and cluster selection is the plane's.
type shape struct {
	// Devices is the whole replica, across every machine it lands on.
	Devices int
	// Tensor is the group that has to fit inside one node. A tensor group reaching over the
	// backend fabric is the one split that does not work, so this is a hard per-node floor
	// rather than a preference.
	Tensor int
	// Groups is how many tensor groups the replica is, which is its pipeline depth.
	Groups int
	// SingleDomain is the declared requirement that every device sit in one fabric.
	SingleDomain bool
}

func shapeOf(dep wire.DeploymentDesired) shape {
	devices := dep.Placement.DevicesPerReplica
	if devices < 1 {
		devices = 1
	}
	tensor := dep.Serving.Parallelism.Tensor
	if tensor < 1 {
		tensor = 1
	}
	if tensor > devices {
		tensor = devices
	}
	groups := devices / tensor
	if devices%tensor != 0 {
		groups++
	}
	return shape{
		Devices:      devices,
		Tensor:       tensor,
		Groups:       groups,
		SingleDomain: dep.Placement.Topology == topologySingleDomain,
	}
}

// topologySingleDomain is the wire's spelling of "this replica lands in one fabric or not at all".
const topologySingleDomain = "single_domain"

// fit is the answer to whether a replica can be placed, and how.
//
// It carries the layout as well as the verdict because the render needs the same numbers: what a
// check concluded and what gets applied have to be the one calculation, or the deployment is
// admitted against one shape and rendered as another.
type fit struct {
	OK bool
	// DevicesPerNode is what one pod asks for. Uniform across the replica's nodes, because a
	// leader-worker group is homogeneous and an engine's ranks are equal.
	DevicesPerNode int
	// NodesPerReplica is how many pods one replica is. One is the ordinary case.
	NodesPerReplica int
	// Domain is the fabric the replica is pinned to, empty when it is not pinned. Set only for
	// a replica that declared single_domain and needs more than one node, which is the only
	// case where the choice is not already made by the node the pod lands on.
	Domain string
	// Reason states the cause when OK is false, in the words a person needs to act on it.
	Reason string
	// Code is the machine-readable half of the same conclusion, reported to the plane.
	Code string
}

// candidate is one usable node reduced to what the fit cares about.
type candidate struct {
	name    string
	domain  string
	devices int64
}

// schedulable reports whether this cluster has room for one replica now, and how it would lay it
// out.
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
// Room means what the scheduler would see: a node's allocatable accelerators less what its
// running pods already request, on nodes whose taints the worker tolerates. The deployment's own
// pods are not counted against it, because a new generation replaces them.
//
// The unit is the tensor group, not the replica. A replica of two tensor groups is two pods that
// may sit on two machines and exchange activations over the fabric, so a cluster of small nodes
// can serve it; a single tensor group of two devices on one-device nodes cannot be served at all,
// however many machines there are.
//
// The reader has to see pods in every namespace, since any of them can hold an accelerator.
func schedulable(
	ctx context.Context,
	c client.Reader,
	dep wire.DeploymentDesired,
	selector map[string]string,
	tolerations []corev1.Toleration,
) (fit, error) {
	want := shapeOf(dep)

	var nodes corev1.NodeList
	if err := c.List(ctx, &nodes); err != nil {
		return fit{}, fmt.Errorf("list nodes: %w", err)
	}
	inUse, err := requestedDevices(ctx, c, dep.DeploymentID)
	if err != nil {
		return fit{}, err
	}
	candidates := make([]candidate, 0, len(nodes.Items))
	var largest, advertised int64
	for i := range nodes.Items {
		node := &nodes.Items[i]
		if !nodeUsable(node) || !matchesSelector(node.Labels, selector) || !tolerated(node, tolerations) {
			continue
		}
		allocatable := node.Status.Allocatable.Name(acceleratorResource, "").Value()
		advertised = max(advertised, allocatable)
		available := max(allocatable-inUse[node.Name], 0)
		largest = max(largest, available)
		if available < 1 {
			continue
		}
		candidates = append(candidates, candidate{name: node.Name, domain: domainOf(node), devices: available})
	}

	// A tensor group is indivisible, so no arrangement of machines helps if none of them holds
	// one.
	if largest < int64(want.Tensor) {
		return fit{
			Code: "NoSchedulableCapacity",
			Reason: fmt.Sprintf(
				"a tensor group of %d accelerators of class %s must fit inside one node and the largest "+
					"usable node has %d free%s",
				want.Tensor, dep.Placement.DeviceClass, largest, devicePluginHint(advertised)),
		}, nil
	}

	if want.SingleDomain {
		return fitWithinOneDomain(want, dep, candidates), nil
	}
	if placed, ok := pack(want, candidates); ok {
		return placed, nil
	}
	return fit{
		Code: "NoSchedulableCapacity",
		Reason: fmt.Sprintf(
			"one replica needs %d accelerators of class %s laid out as %d tensor groups of %d, and no "+
				"set of usable nodes has them free%s",
			want.Devices, dep.Placement.DeviceClass, want.Groups, want.Tensor, devicePluginHint(advertised)),
	}, nil
}

// requestedDevices sums, per node, the accelerators requested by pods that hold them: bound to a
// node and not finished. Pods of the deployment being placed are left out.
func requestedDevices(ctx context.Context, c client.Reader, deploymentID string) (map[string]int64, error) {
	var pods corev1.PodList
	if err := c.List(ctx, &pods); err != nil {
		return nil, fmt.Errorf("list pods: %w", err)
	}
	out := map[string]int64{}
	for i := range pods.Items {
		pod := &pods.Items[i]
		if pod.Spec.NodeName == "" || pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
			continue
		}
		if deploymentID != "" && pod.Labels[deploymentIDLabel] == deploymentID {
			continue
		}
		out[pod.Spec.NodeName] += podDevices(pod)
	}
	return out, nil
}

// podDevices is what the scheduler charges a pod for accelerators: the larger of its containers'
// sum and its largest init container, each read from requests and falling back to limits, which
// is where an extended resource is usually declared.
func podDevices(pod *corev1.Pod) int64 {
	asked := func(c corev1.Container) int64 {
		if q, ok := c.Resources.Requests[acceleratorResource]; ok {
			return q.Value()
		}
		if q, ok := c.Resources.Limits[acceleratorResource]; ok {
			return q.Value()
		}
		return 0
	}
	var sum, initMax int64
	for _, c := range pod.Spec.Containers {
		sum += asked(c)
	}
	for _, c := range pod.Spec.InitContainers {
		initMax = max(initMax, asked(c))
	}
	return max(sum, initMax)
}

// tolerated reports whether every taint that keeps a pod off a node is tolerated.
func tolerated(node *corev1.Node, tolerations []corev1.Toleration) bool {
	for i := range node.Spec.Taints {
		taint := &node.Spec.Taints[i]
		if taint.Effect != corev1.TaintEffectNoSchedule && taint.Effect != corev1.TaintEffectNoExecute {
			continue
		}
		ok := false
		for j := range tolerations {
			if tolerations[j].ToleratesTaint(taint) {
				ok = true
				break
			}
		}
		if !ok {
			return false
		}
	}
	return true
}

// fitWithinOneDomain answers the same question with the replica confined to one fabric.
//
// Every domain is tried and the first that holds the replica wins. Which one is then recorded on
// the fit, because a replica spanning machines inside a domain has to be told which domain — the
// scheduler is free to spread pods over every fabric in the cluster otherwise, and a gang split
// across two fabrics is exactly what the declaration was made to prevent.
func fitWithinOneDomain(want shape, dep wire.DeploymentDesired, candidates []candidate) fit {
	byDomain := map[string][]candidate{}
	order := []string{}
	for _, node := range candidates {
		if _, seen := byDomain[node.domain]; !seen {
			order = append(order, node.domain)
		}
		byDomain[node.domain] = append(byDomain[node.domain], node)
	}
	sort.Strings(order)

	var largest int64
	for _, domain := range order {
		var held int64
		for _, node := range byDomain[domain] {
			held += node.devices
		}
		if held > largest {
			largest = held
		}
		if placed, ok := pack(want, byDomain[domain]); ok {
			if placed.NodesPerReplica > 1 {
				placed.Domain = domain
			}
			return placed
		}
	}

	return fit{
		Code: "NoTopologyDomain",
		Reason: fmt.Sprintf(
			"one replica needs %d accelerators of class %s inside a single topology domain and the "+
				"largest usable domain holds %d; a node with no %s label is a domain of its own, so "+
				"nodes with no declared fabric between them cannot be combined",
			want.Devices, dep.Placement.DeviceClass, largest, cliqueLabel),
	}
}

// pack lays the replica's tensor groups over a set of nodes, densest first.
//
// Densest first because a replica that fits on one machine belongs on one machine: every node it
// spans is another hop on the critical path between pipeline stages, and spreading a replica that
// did not need spreading buys nothing and costs latency.
//
// The per-node count is uniform. Kubernetes would happily run one pod with four accelerators
// beside one with two, and no engine would: the ranks of a pipeline are equal, and a leader-worker
// group is rendered from one pod template.
func pack(want shape, candidates []candidate) (fit, bool) {
	var densest int64
	for _, node := range candidates {
		if groups := node.devices / int64(want.Tensor); groups > densest {
			densest = groups
		}
	}
	if densest < 1 {
		return fit{}, false
	}
	perNode := densest
	if perNode > int64(want.Groups) {
		perNode = int64(want.Groups)
	}

	// Walk the per-node group count down. The densest packing is preferred, but a cluster can
	// hold one very large node and several ordinary ones, and insisting on the largest would
	// refuse a layout that the ordinary ones serve between them.
	for ; perNode >= 1; perNode-- {
		needed := int64(want.Groups) / perNode
		if int64(want.Groups)%perNode != 0 {
			needed++
		}
		devicesPerNode := perNode * int64(want.Tensor)
		var have int64
		for _, node := range candidates {
			if node.devices >= devicesPerNode {
				have++
			}
		}
		if have >= needed {
			return fit{
				OK:              true,
				DevicesPerNode:  int(devicesPerNode),
				NodesPerReplica: int(needed),
			}, true
		}
	}
	return fit{}, false
}

// domainOf names the fabric a node's accelerators belong to, or the node itself when nothing has
// asserted one.
func domainOf(node *corev1.Node) string {
	if clique := node.Labels[cliqueLabel]; clique != "" {
		return "nvlink/" + clique
	}
	return "node/" + node.Name
}

// devicePluginHint adds the cause nobody thinks to check, and only when it is possible.
//
// Appended when no usable node advertises a single accelerator, because that is the signature of
// a device plugin that is not installed and it is indistinguishable from having no GPUs at all.
// A cluster advertising some accelerators has a plugin, and saying otherwise sends a person to
// look at the one thing that is demonstrably working.
func devicePluginHint(largest int64) string {
	if largest > 0 {
		return ""
	}
	return "; if the nodes are present, the device plugin may not be installed"
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
